import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ApiError } from "./api";
import { AuthProvider, useAuth } from "./auth";
import { AppShell } from "./components/AppShell";
import { LoadingBlock } from "./components/Feedback";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { DocumentUploadPage } from "./pages/DocumentUploadPage";
import { LoginPage } from "./pages/LoginPage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { ProjectCreatePage } from "./pages/ProjectCreatePage";
import { ProjectWorkbenchPage } from "./pages/ProjectWorkbenchPage";
import { RecordsPage } from "./pages/RecordsPage";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { TaskQueuePage } from "./pages/TaskQueuePage";
import { Link, NavigationProvider, useNavigation } from "./navigation";
import type { ProjectRecordSection, RuntimeConfig } from "./types";

const recordSections = new Set<ProjectRecordSection>([
  "DOCUMENTS",
  "EVIDENCE",
  "BOQ_SCOPE",
  "PRICING",
  "CONTRACT_RISK",
  "CALCULATION",
  "APPROVALS",
  "ACTUALS",
  "GOVERNANCE",
  "AUDIT",
]);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (failureCount, error) => {
        if (
          error instanceof ApiError &&
          [400, 401, 403, 404, 409, 422].includes(error.status)
        ) {
          return false;
        }
        return failureCount < 2;
      },
    },
  },
});

function AuthenticatedRoutes({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const { pathname } = useNavigation();

  if (pathname === "/auth/callback") {
    return <AuthCallbackPage />;
  }
  if (pathname === "/auth/signout-callback") {
    return <AuthCallbackPage signOut />;
  }
  if (auth.status === "LOADING") {
    return (
      <main className="bootstrap-screen">
        <LoadingBlock label="Проверка защищённого сеанса" />
      </main>
    );
  }
  if (auth.status !== "AUTHENTICATED") {
    return <LoginPage />;
  }

  let page: ReactNode = null;
  if (pathname === "/") {
    page = <PortfolioPage config={config} />;
  } else if (pathname === "/tasks") {
    page = <TaskQueuePage config={config} />;
  } else if (pathname === "/projects/new") {
    page = <ProjectCreatePage config={config} />;
  } else {
    const parts = pathname.split("/").filter(Boolean);
    if (parts[0] === "tasks" && parts.length === 2) {
      let taskId: string | null = null;
      try {
        taskId = decodeURIComponent(parts[1] ?? "");
      } catch {
        taskId = null;
      }
      if (taskId !== null && taskId !== "") {
        page = <TaskDetailPage config={config} taskId={taskId} />;
      }
    } else if (parts[0] === "projects" && parts.length >= 2) {
      let projectId: string | null = null;
      try {
        projectId = decodeURIComponent(parts[1] ?? "");
      } catch {
        projectId = null;
      }
      const rawSection = parts[2];
      if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "documents" &&
        parts[3] === "new"
      ) {
        page = <DocumentUploadPage config={config} projectId={projectId} />;
      } else if (projectId !== null && projectId !== "" && parts.length === 2) {
        page = <ProjectWorkbenchPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 3 &&
        rawSection !== undefined &&
        recordSections.has(rawSection as ProjectRecordSection)
      ) {
        page = (
          <RecordsPage
            config={config}
            projectId={projectId}
            section={rawSection as ProjectRecordSection}
          />
        );
      }
    }
  }
  page ??= (
    <div className="page">
      <section className="error-block" role="alert">
        <div>
          <h1>Маршрут не найден</h1>
          <p>Запрошенный экран не относится к рабочему контуру TenderGuard.</p>
        </div>
        <Link className="button button--secondary" to="/">
          К проектам
        </Link>
      </section>
    </div>
  );

  return <AppShell config={config}>{page}</AppShell>;
}

export function App({ config }: { config: RuntimeConfig }) {
  return (
    <QueryClientProvider client={queryClient}>
      <NavigationProvider>
        <AuthProvider config={config}>
          <AuthenticatedRoutes config={config} />
        </AuthProvider>
      </NavigationProvider>
    </QueryClientProvider>
  );
}
