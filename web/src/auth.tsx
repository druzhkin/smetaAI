import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";

import type { ActorRole, DevelopmentIdentity, RuntimeConfig } from "./types";

const DEV_IDENTITY_KEY = "tenderguard.development-identity";
const allowedRoles = new Set<ActorRole>([
  "ESTIMATOR",
  "PROCUREMENT",
  "TECHNICAL_EXPERT",
  "REVIEWER",
  "APPROVER",
  "METHODOLOGY_OWNER",
  "CATALOG_OWNER",
  "AUDITOR",
  "ADMIN",
]);

type AuthenticationStatus =
  "LOADING" | "AUTHENTICATED" | "UNAUTHENTICATED" | "UNAVAILABLE" | "ERROR";

interface AuthContextValue {
  status: AuthenticationStatus;
  mode: RuntimeConfig["authentication_mode"];
  displayName: string | null;
  roles: ActorRole[];
  error: string | null;
  authorizationHeaders: () => Record<string, string>;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  completeSignIn: () => Promise<void>;
  completeSignOut: () => Promise<void>;
  setDevelopmentIdentity: (identity: DevelopmentIdentity) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function parseDevelopmentIdentity(
  value: string | null,
): DevelopmentIdentity | null {
  if (value === null) {
    return null;
  }
  try {
    const parsed = JSON.parse(value) as Partial<DevelopmentIdentity>;
    if (
      typeof parsed.actorId !== "string" ||
      !parsed.actorId.trim() ||
      typeof parsed.organizationId !== "string" ||
      !parsed.organizationId.trim() ||
      !Array.isArray(parsed.roles) ||
      parsed.roles.length === 0 ||
      !parsed.roles.every(
        (role): role is ActorRole =>
          typeof role === "string" && allowedRoles.has(role as ActorRole),
      )
    ) {
      return null;
    }
    return {
      actorId: parsed.actorId.trim(),
      organizationId: parsed.organizationId.trim(),
      roles: [...new Set(parsed.roles)],
    };
  } catch {
    return null;
  }
}

function rolesFromUser(user: User): ActorRole[] {
  const claims = user.profile as Record<string, unknown>;
  const candidates: unknown[] = [];
  if (Array.isArray(claims.roles)) {
    candidates.push(...claims.roles);
  }
  const realmAccess = claims.realm_access;
  if (
    typeof realmAccess === "object" &&
    realmAccess !== null &&
    "roles" in realmAccess &&
    Array.isArray((realmAccess as { roles: unknown }).roles)
  ) {
    candidates.push(...(realmAccess as { roles: unknown[] }).roles);
  }
  return [
    ...new Set(
      candidates.filter(
        (role): role is ActorRole =>
          typeof role === "string" && allowedRoles.has(role as ActorRole),
      ),
    ),
  ];
}

function createUserManager(config: RuntimeConfig): UserManager | null {
  if (
    config.authentication_mode !== "OIDC" ||
    config.oidc_authority === null ||
    config.oidc_client_id === null
  ) {
    return null;
  }
  return new UserManager({
    authority: config.oidc_authority,
    client_id: config.oidc_client_id,
    redirect_uri: `${window.location.origin}/auth/callback`,
    post_logout_redirect_uri: `${window.location.origin}/auth/signout-callback`,
    response_type: "code",
    scope: config.oidc_scope,
    loadUserInfo: false,
    automaticSilentRenew: false,
    monitorSession: true,
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
}

export function AuthProvider({
  config,
  children,
}: {
  config: RuntimeConfig;
  children: ReactNode;
}) {
  const userManager = useMemo(() => createUserManager(config), [config]);
  const [user, setUser] = useState<User | null>(null);
  const [developmentIdentity, setDevelopmentIdentityState] =
    useState<DevelopmentIdentity | null>(() =>
      parseDevelopmentIdentity(sessionStorage.getItem(DEV_IDENTITY_KEY)),
    );
  const [status, setStatus] = useState<AuthenticationStatus>("LOADING");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (config.authentication_mode === "DEVELOPMENT") {
      setStatus(
        developmentIdentity === null ? "UNAUTHENTICATED" : "AUTHENTICATED",
      );
      return;
    }
    if (config.authentication_mode === "UNAVAILABLE" || userManager === null) {
      setStatus("UNAVAILABLE");
      return;
    }

    let active = true;
    const loaded = (nextUser: User) => {
      if (active) {
        setUser(nextUser);
        setStatus(nextUser.expired ? "UNAUTHENTICATED" : "AUTHENTICATED");
      }
    };
    const unloaded = () => {
      if (active) {
        setUser(null);
        setStatus("UNAUTHENTICATED");
      }
    };
    userManager.events.addUserLoaded(loaded);
    userManager.events.addUserUnloaded(unloaded);
    void userManager
      .getUser()
      .then((currentUser) => {
        if (!active) {
          return;
        }
        setUser(currentUser);
        setStatus(
          currentUser !== null && !currentUser.expired
            ? "AUTHENTICATED"
            : "UNAUTHENTICATED",
        );
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Не удалось проверить сеанс",
          );
          setStatus("ERROR");
        }
      });
    return () => {
      active = false;
      userManager.events.removeUserLoaded(loaded);
      userManager.events.removeUserUnloaded(unloaded);
    };
  }, [config.authentication_mode, developmentIdentity, userManager]);

  const setDevelopmentIdentity = useCallback(
    (identity: DevelopmentIdentity) => {
      const normalized: DevelopmentIdentity = {
        actorId: identity.actorId.trim(),
        organizationId: identity.organizationId.trim(),
        roles: [...new Set(identity.roles)],
      };
      sessionStorage.setItem(DEV_IDENTITY_KEY, JSON.stringify(normalized));
      setDevelopmentIdentityState(normalized);
      setStatus("AUTHENTICATED");
    },
    [],
  );

  const authorizationHeaders = useCallback((): Record<string, string> => {
    if (config.authentication_mode === "DEVELOPMENT") {
      if (developmentIdentity === null) {
        return {};
      }
      return {
        "X-Dev-Actor": developmentIdentity.actorId,
        "X-Dev-Organization": developmentIdentity.organizationId,
        "X-Dev-Roles": developmentIdentity.roles.join(","),
      };
    }
    if (user === null || user.expired) {
      return {};
    }
    return { Authorization: `${user.token_type} ${user.access_token}` };
  }, [config.authentication_mode, developmentIdentity, user]);

  const signIn = useCallback(async () => {
    setError(null);
    if (config.authentication_mode === "OIDC" && userManager !== null) {
      await userManager.signinRedirect();
    }
  }, [config.authentication_mode, userManager]);

  const signOut = useCallback(async () => {
    if (config.authentication_mode === "DEVELOPMENT") {
      sessionStorage.removeItem(DEV_IDENTITY_KEY);
      setDevelopmentIdentityState(null);
      setStatus("UNAUTHENTICATED");
      return;
    }
    if (userManager !== null) {
      await userManager.signoutRedirect();
    }
  }, [config.authentication_mode, userManager]);

  const completeSignIn = useCallback(async () => {
    if (userManager === null) {
      throw new Error("OIDC-клиент не настроен");
    }
    const authenticatedUser = await userManager.signinRedirectCallback();
    setUser(authenticatedUser);
    setStatus("AUTHENTICATED");
  }, [userManager]);

  const completeSignOut = useCallback(async () => {
    if (userManager !== null) {
      await userManager.signoutRedirectCallback();
      await userManager.removeUser();
    }
    setUser(null);
    setStatus("UNAUTHENTICATED");
  }, [userManager]);

  const displayName =
    config.authentication_mode === "DEVELOPMENT"
      ? (developmentIdentity?.actorId ?? null)
      : (user?.profile.name ??
        user?.profile.preferred_username ??
        user?.profile.sub ??
        null);
  const roles =
    config.authentication_mode === "DEVELOPMENT"
      ? (developmentIdentity?.roles ?? [])
      : user === null
        ? []
        : rolesFromUser(user);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      mode: config.authentication_mode,
      displayName,
      roles,
      error,
      authorizationHeaders,
      signIn,
      signOut,
      completeSignIn,
      completeSignOut,
      setDevelopmentIdentity,
    }),
    [
      status,
      config.authentication_mode,
      displayName,
      roles,
      error,
      authorizationHeaders,
      signIn,
      signOut,
      completeSignIn,
      completeSignOut,
      setDevelopmentIdentity,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}
