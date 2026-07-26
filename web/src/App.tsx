import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, type ReactNode } from "react";

import { ApiError } from "./api";
import { AuthProvider, useAuth } from "./auth";
import { AppShell } from "./components/AppShell";
import { LoadingBlock } from "./components/Feedback";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { CalculationPage } from "./pages/CalculationPage";
import { ConflictResolvePage } from "./pages/ConflictResolvePage";
import { DocumentSetConfirmPage } from "./pages/DocumentSetConfirmPage";
import { DocumentUploadPage } from "./pages/DocumentUploadPage";
import { LoginPage } from "./pages/LoginPage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { PriceItemPage } from "./pages/PriceItemPage";
import { ProjectCreatePage } from "./pages/ProjectCreatePage";
import { ProjectWorkbenchPage } from "./pages/ProjectWorkbenchPage";
import { QuantityChangeProposePage } from "./pages/QuantityChangeProposePage";
import { QuantityManualChangePage } from "./pages/QuantityManualChangePage";
import { RecordsPage } from "./pages/RecordsPage";
import { ReleasePage } from "./pages/ReleasePage";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { TaskQueuePage } from "./pages/TaskQueuePage";
import { Link, NavigationProvider, useNavigation } from "./navigation";
import type { ProjectRecordSection, RuntimeConfig } from "./types";

const ManualEvidenceEntryPage = lazy(async () => ({
  default: (await import("./pages/ManualEvidenceEntryPage"))
    .ManualEvidenceEntryPage,
}));
const ManualEvidenceReviewPage = lazy(async () => ({
  default: (await import("./pages/ManualEvidenceReviewPage"))
    .ManualEvidenceReviewPage,
}));
const ReconciliationPage = lazy(async () => ({
  default: (await import("./pages/ReconciliationPage")).ReconciliationPage,
}));
const PassportPage = lazy(async () => ({
  default: (await import("./pages/PassportPage")).PassportPage,
}));
const BoqAuthoringPage = lazy(async () => ({
  default: (await import("./pages/BoqAuthoringPage")).BoqAuthoringPage,
}));
const BoqLineReviewPage = lazy(async () => ({
  default: (await import("./pages/BoqLineReviewPage")).BoqLineReviewPage,
}));
const ScopeCompletenessPage = lazy(async () => ({
  default: (await import("./pages/ScopeCompletenessPage"))
    .ScopeCompletenessPage,
}));
const NomenclatureAssessmentPage = lazy(async () => ({
  default: (await import("./pages/NomenclatureAssessmentPage"))
    .NomenclatureAssessmentPage,
}));
const NomenclatureReviewPage = lazy(async () => ({
  default: (await import("./pages/NomenclatureReviewPage"))
    .NomenclatureReviewPage,
}));
const ScenarioComparisonPage = lazy(async () => ({
  default: (await import("./pages/ScenarioComparisonPage"))
    .ScenarioComparisonPage,
}));

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
        rawSection === "passport" &&
        parts[3] === "manage"
      ) {
        page = <PassportPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "evidence" &&
        parts[3] === "reconcile"
      ) {
        page = <ReconciliationPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "boq" &&
        parts[3] === "new"
      ) {
        page = <BoqAuthoringPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "boq" &&
        parts[3] === "scope-review"
      ) {
        page = <ScopeCompletenessPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "boq-lines" &&
        parts[4] === "review"
      ) {
        let lineId: string | null = null;
        try {
          lineId = decodeURIComponent(parts[3] ?? "");
        } catch {
          lineId = null;
        }
        if (lineId !== null && lineId !== "") {
          page = (
            <BoqLineReviewPage
              config={config}
              projectId={projectId}
              lineId={lineId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "nomenclature" &&
        parts[3] === "new"
      ) {
        page = (
          <NomenclatureAssessmentPage config={config} projectId={projectId} />
        );
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "nomenclature" &&
        parts[4] === "review"
      ) {
        let matchId: string | null = null;
        try {
          matchId = decodeURIComponent(parts[3] ?? "");
        } catch {
          matchId = null;
        }
        if (matchId !== null && matchId !== "") {
          page = (
            <NomenclatureReviewPage
              config={config}
              projectId={projectId}
              matchId={matchId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "boq-lines" &&
        parts[4] === "quantity-change"
      ) {
        let lineId: string | null = null;
        try {
          lineId = decodeURIComponent(parts[3] ?? "");
        } catch {
          lineId = null;
        }
        if (lineId !== null && lineId !== "") {
          page = (
            <QuantityChangeProposePage
              config={config}
              projectId={projectId}
              lineId={lineId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "manual-changes"
      ) {
        let changeId: string | null = null;
        try {
          changeId = decodeURIComponent(parts[3] ?? "");
        } catch {
          changeId = null;
        }
        if (changeId !== null && changeId !== "") {
          page = (
            <QuantityManualChangePage
              config={config}
              projectId={projectId}
              changeId={changeId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "evidence" &&
        parts[3] === "manual"
      ) {
        page = (
          <ManualEvidenceEntryPage config={config} projectId={projectId} />
        );
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 6 &&
        rawSection === "evidence" &&
        parts[3] === "observations" &&
        parts[5] === "review"
      ) {
        let observationId: string | null = null;
        try {
          observationId = decodeURIComponent(parts[4] ?? "");
        } catch {
          observationId = null;
        }
        if (observationId !== null && observationId !== "") {
          page = (
            <ManualEvidenceReviewPage
              config={config}
              projectId={projectId}
              observationId={observationId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "pricing" &&
        parts[3] === "items"
      ) {
        let itemId: string | null = null;
        try {
          itemId = decodeURIComponent(parts[4] ?? "");
        } catch {
          itemId = null;
        }
        if (itemId !== null && itemId !== "") {
          page = (
            <PriceItemPage
              config={config}
              projectId={projectId}
              itemId={itemId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "conflicts" &&
        parts[4] === "resolve"
      ) {
        let conflictId: string | null = null;
        try {
          conflictId = decodeURIComponent(parts[3] ?? "");
        } catch {
          conflictId = null;
        }
        if (conflictId !== null && conflictId !== "") {
          page = (
            <ConflictResolvePage
              config={config}
              projectId={projectId}
              conflictId={conflictId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 5 &&
        rawSection === "document-sets" &&
        parts[4] === "confirm"
      ) {
        let documentSetId: string | null = null;
        try {
          documentSetId = decodeURIComponent(parts[3] ?? "");
        } catch {
          documentSetId = null;
        }
        if (documentSetId !== null && documentSetId !== "") {
          page = (
            <DocumentSetConfirmPage
              config={config}
              projectId={projectId}
              documentSetId={documentSetId}
            />
          );
        }
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "documents" &&
        parts[3] === "new"
      ) {
        page = <DocumentUploadPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 3 &&
        rawSection === "release"
      ) {
        page = <ReleasePage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 3 &&
        rawSection === "scenarios"
      ) {
        page = <ScenarioComparisonPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 3 &&
        rawSection === "CALCULATION"
      ) {
        page = <CalculationPage config={config} projectId={projectId} />;
      } else if (
        projectId !== null &&
        projectId !== "" &&
        parts.length === 4 &&
        rawSection === "CALCULATION" &&
        parts[3] === "records"
      ) {
        page = (
          <RecordsPage
            config={config}
            projectId={projectId}
            section="CALCULATION"
          />
        );
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

  return (
    <AppShell config={config}>
      <Suspense
        fallback={
          <div className="page">
            <LoadingBlock label="Загрузка защищённого рабочего экрана" />
          </div>
        }
      >
        {page}
      </Suspense>
    </AppShell>
  );
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
