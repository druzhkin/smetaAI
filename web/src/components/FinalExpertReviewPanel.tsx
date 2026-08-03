import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getBoqPriceMatrix,
  newIdempotencyKey,
  requestFinalExpertRework,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import {
  finalReviewCandidates,
  validateFinalRework,
} from "../finalReviewWorkflow";
import type { ReleaseTarget } from "../releaseWorkflow";
import type { GateDecision, ProjectView } from "../types";
import { ErrorBlock, LoadingBlock } from "./Feedback";
import { Icon } from "./Icon";
import { StatusPill } from "./StatusPill";

export function FinalExpertReviewPanel({
  context,
  project,
  target,
  decision,
  gateHash,
}: {
  context: RequestContext;
  project: ProjectView;
  target: ReleaseTarget;
  decision: GateDecision;
  gateHash: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const hasExpertRole = auth.roles.some(
    (role) => role === "REVIEWER" || role === "APPROVER",
  );
  const enabled = project.state === "EXPERT_REVIEW" && hasExpertRole;
  const matrixQuery = useQuery({
    queryKey: ["boq-price-matrix", project.id, "final-review"],
    queryFn: ({ signal }) => getBoqPriceMatrix(context, project.id, signal),
    enabled,
  });
  const candidates = useMemo(
    () =>
      matrixQuery.data === undefined
        ? []
        : finalReviewCandidates(matrixQuery.data, decision),
    [decision, matrixQuery.data],
  );
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      const selected = candidates.filter((candidate) =>
        selectedKeys.has(candidate.key),
      );
      return requestFinalExpertRework(context, {
        projectId: project.id,
        gateTarget: target,
        expectedProjectRowVersion: project.row_version,
        gateHash,
        issues: selected.map((candidate) => ({
          kind: candidate.kind,
          reference_id: candidate.reference_id,
          code: candidate.code,
          comment: reason.trim(),
        })),
        reason: reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      setSelectedKeys(new Set());
      setReason("");
      setFormError(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["release-gates", project.id],
        }),
        queryClient.invalidateQueries({ queryKey: ["project", project.id] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", project.id] }),
        queryClient.invalidateQueries({ queryKey: ["projects"] }),
        queryClient.invalidateQueries({
          queryKey: ["boq-price-matrix", project.id],
        }),
        queryClient.invalidateQueries({
          queryKey: ["automation-rework-status", project.id],
        }),
      ]);
    },
  });

  if (!enabled) {
    return null;
  }
  if (matrixQuery.isPending) {
    return <LoadingBlock label="Подготовка итогового пакета для эксперта" />;
  }
  if (matrixQuery.isError) {
    return (
      <ErrorBlock
        error={matrixQuery.error}
        onRetry={() => void matrixQuery.refetch()}
      />
    );
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const validationError = validateFinalRework(selectedKeys, reason);
    setFormError(validationError);
    if (validationError !== null) {
      return;
    }
    try {
      mutation.mutate(newIdempotencyKey());
    } catch (error) {
      setFormError(
        error instanceof Error
          ? error.message
          : "Не удалось создать ключ операции",
      );
    }
  };
  const toggle = (key: string) => {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
    setFormError(null);
    mutation.reset();
  };

  return (
    <section
      className="final-expert-review"
      aria-labelledby="final-review-title"
    >
      <header className="section-heading">
        <div>
          <p className="eyebrow">Единственное ручное действие</p>
          <h2 id="final-review-title">Итоговая проверка эксперта</h2>
          <p>
            Система уже собрала и рассчитала всё доступное. Примите готовый
            результат выше либо отметьте конкретные строки и верните их в
            автоматический расчёт.
          </p>
        </div>
        <span>{candidates.length}</span>
      </header>

      <div className="final-review-summary">
        <div>
          <strong>{matrixQuery.data.rows.length}</strong>
          <span>строк расчёта</span>
        </div>
        <div>
          <strong>{matrixQuery.data.blocked_row_count}</strong>
          <span>заблокировано системой</span>
        </div>
        <div>
          <strong>{decision.findings.length}</strong>
          <span>общих блокировок</span>
        </div>
      </div>

      <form className="entry-form final-rework-form" onSubmit={submit}>
        <div className="final-review-candidates">
          {candidates.map((candidate) => (
            <label
              className={`final-review-candidate ${
                selectedKeys.has(candidate.key)
                  ? "final-review-candidate--selected"
                  : ""
              }`}
              key={candidate.key}
            >
              <input
                type="checkbox"
                checked={selectedKeys.has(candidate.key)}
                onChange={() => toggle(candidate.key)}
              />
              <span className="final-review-candidate__body">
                <span className="final-review-candidate__heading">
                  <strong>{candidate.label}</strong>
                  <StatusPill
                    value={candidate.blocked ? "BLOCKED" : "VERIFIED"}
                  />
                </span>
                <span>{candidate.detail}</span>
                <code>
                  {candidate.kind} · {candidate.reference_id} · {candidate.code}
                </code>
              </span>
            </label>
          ))}
        </div>

        <label>
          Что именно нужно проверить заново
          <textarea
            value={reason}
            rows={4}
            maxLength={2000}
            placeholder="Например: повторно найти рыночные цены у производителей и проверить доставку до объекта."
            onChange={(event) => {
              setReason(event.target.value);
              setFormError(null);
              mutation.reset();
            }}
          />
        </label>

        {(formError ?? (mutation.isError ? mutation.error.message : null)) !==
          null && (
          <p className="form-error" role="alert">
            {formError ?? (mutation.isError ? mutation.error.message : null)}
          </p>
        )}
        {mutation.isSuccess && (
          <div className="success-panel" role="status">
            <Icon name="check" size={22} />
            <div>
              <strong>Доработка поставлена в автоматическую очередь</strong>
              <p>
                Проект переведён в {mutation.data.target_stage}. Старый расчёт
                больше нельзя выпустить.
              </p>
            </div>
          </div>
        )}
        <button
          className="button button--secondary"
          type="submit"
          disabled={mutation.isPending}
        >
          <Icon name="refresh" size={17} />
          {mutation.isPending
            ? "Фиксация и возврат…"
            : `Вернуть выбранное на автоматическую доработку (${selectedKeys.size})`}
        </button>
      </form>
    </section>
  );
}
