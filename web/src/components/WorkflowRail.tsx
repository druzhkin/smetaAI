import type { ApprovalState } from "../types";
import { Link } from "../navigation";
import { workflowStages, workflowStatusFor } from "../workflow";
import { Icon } from "./Icon";

const stageStatusLabels = {
  complete: "Этап завершён",
  current: "Текущий этап",
  pending: "Этап ещё не начат",
  blocked: "Путь остановлен блокирующей проверкой",
  closed: "Проект закрыт",
} as const;

export function WorkflowRail({
  state,
  projectId,
}: {
  state?: ApprovalState;
  projectId?: string;
}) {
  return (
    <ol className="workflow-rail" aria-label="Путь расчёта проекта">
      {workflowStages.map((stage, stageIndex) => {
        const status = workflowStatusFor(state, stageIndex);
        const content = (
          <>
            <div className="workflow-rail__number" aria-hidden="true">
              {status === "complete" ? (
                <Icon name="check" size={17} />
              ) : (
                stage.index
              )}
            </div>
            <div className="workflow-rail__content">
              <span className="workflow-rail__owner">{stage.owner}</span>
              <strong>{stage.title}</strong>
              <p>{stage.description}</p>
              {state !== undefined && status !== "blocked" && (
                <small>{stageStatusLabels[status]}</small>
              )}
            </div>
            {projectId !== undefined && <Icon name="arrow" size={15} />}
          </>
        );
        return (
          <li
            key={stage.id}
            className={`workflow-rail__stage workflow-rail__stage--${status}`}
            data-stage-status={status}
          >
            {projectId === undefined ? (
              <div className="workflow-rail__link">{content}</div>
            ) : (
              <Link
                className="workflow-rail__link"
                to={`/projects/${encodeURIComponent(projectId)}/${stage.path}`}
                aria-current={status === "current" ? "step" : undefined}
              >
                {content}
              </Link>
            )}
          </li>
        );
      })}
    </ol>
  );
}
