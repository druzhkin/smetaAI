import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";

interface NavigationContextValue {
  pathname: string;
  navigate: (to: string, options?: { replace?: boolean }) => void;
}

const NavigationContext = createContext<NavigationContextValue | null>(null);

export function safeInternalPath(to: string): string {
  if (
    !to.startsWith("/") ||
    to.startsWith("//") ||
    to.includes("\\") ||
    to.includes("\u0000")
  ) {
    throw new Error("Navigation target must be a same-origin absolute path");
  }
  return to;
}

export function NavigationProvider({ children }: { children: ReactNode }) {
  const [pathname, setPathname] = useState(window.location.pathname);

  useEffect(() => {
    const handlePopState = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = useCallback(
    (to: string, options?: { replace?: boolean }) => {
      const safePath = safeInternalPath(to);
      if (options?.replace === true) {
        window.history.replaceState(null, "", safePath);
      } else {
        window.history.pushState(null, "", safePath);
      }
      setPathname(window.location.pathname);
      window.scrollTo({ top: 0, behavior: "instant" });
    },
    [],
  );

  const value = useMemo(() => ({ pathname, navigate }), [navigate, pathname]);
  return (
    <NavigationContext.Provider value={value}>
      {children}
    </NavigationContext.Provider>
  );
}

export function useNavigation(): NavigationContextValue {
  const value = useContext(NavigationContext);
  if (value === null) {
    throw new Error("useNavigation must be used inside NavigationProvider");
  }
  return value;
}

export function Link({
  to,
  onClick,
  ...props
}: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  to: string;
}) {
  const { navigate } = useNavigation();
  const safePath = safeInternalPath(to);

  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigate(safePath);
  }

  return <a {...props} href={safePath} onClick={handleClick} />;
}

export function NavLink({
  to,
  end = false,
  className,
  ...props
}: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "className"> & {
  to: string;
  end?: boolean;
  className: (state: { isActive: boolean }) => string;
}) {
  const { pathname } = useNavigation();
  const isActive = end
    ? pathname === to
    : pathname === to || pathname.startsWith(`${to}/`);
  return <Link {...props} to={to} className={className({ isActive })} />;
}
