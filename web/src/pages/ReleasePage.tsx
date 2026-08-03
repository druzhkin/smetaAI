import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  attemptRelease,
  getReleaseGates,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { AutomationReworkStatusPanel } from "../components/AutomationReworkStatusPanel";
import { FinalExpertReviewPanel } from "../components/FinalExpertReviewPanel";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { findingLabels } from "../labels";
import { Link } from "../navigation";
import {
  releaseStateEligible,
  releaseTargetState,
  validateReleaseDraft,
  type ReleaseDraft,
  type ReleaseTarget,
} from "../releaseWorkflow";
import type { GateDecision, RuntimeConfig } from "../types";

const initialDraft: ReleaseDraft = {
  target: "bid",
  reason: "",
  projectCode: "",
  targetState: "",
  acknowledged: false,
};

const targetLabels: Record<ReleaseTarget, string> = {
  bid: "Конкурсная цена",
  internal: "Внутреннее использование",
};

function GatePanel({
  target,
  decision,
  gateHash,
  selected,
  eligible,
  onSelect,
}: {
  target: ReleaseTarget;
  decision: GateDecision;
  gateHash: string;
  selected: boolean;
  eligible: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`release-target ${selected ? "release-target--selected" : ""}`}
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="release-target__index">
        {target === "bid" ? "BID" : "INT"}
      </span>
      <span className="release-target__body">
        <span className="release-target__heading">
          <strong>{targetLabels[target]}</strong>
          <StatusPill
            value={
              decision.allowed && eligible
                ? "READY_FOR_DECISION"
                : decision.resulting_state
            }
          />
        </span>
        <span>
          {decision.allowed
            ? eligible
              ? "Все hard stops пройдены; требуется контролируемое решение утверждающего."
              : "Проверки пройдены, но текущий workflow не допускает этот переход."
            : `${decision.findings.length} блокирующих проверок не пройдено.`}
        </span>
        <code>{gateHash}</code>
      </span>
    </button>
  );
}

export function ReleasePage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ReleaseDraft>(initialDraft);
  const [operationKey, setOperationKey] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const requestContext = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const gatesQuery = useQuery({
    queryKey: ["release-gates", projectId],
    queryFn: ({ signal }) => getReleaseGates(requestContext, projectId, signal),
    refetchOnWindowFocus: true,
  });
  const mutation = useMutation({
    mutationFn: (input: {
      target: ReleaseTarget;
      expectedRowVersion: number;
      gateHash: string;
      reason: string;
      idempotencyKey: string;
    }) =>
      attemptRelease(requestContext, {
        projectId,
        ...input,
      }),
    onSuccess: async () => {
      setOperationKey(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["release-gates", projectId],
        }),
        queryClient.invalidateQueries({ queryKey: ["project", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["projects"] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "APPROVALS"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "AUDIT"],
        }),
      ]);
    },
  });

  if (gatesQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Независимая проверка hard stops" />
      </div>
    );
  }
  if (gatesQuery.isError) {
    return (
      <div className="page">
        <ErrorBlock
          error={gatesQuery.error}
          onRetry={() => void gatesQuery.refetch()}
        />
      </div>
    );
  }

  const gates = gatesQuery.data;
  const decision =
    draft.target === "bid" ? gates.decision : gates.internal_decision;
  const gateHash =
    draft.target === "bid" ? gates.gate_hash : gates.internal_gate_hash;
  const hasApproverRole = auth.roles.includes("APPROVER");
  const eligible = releaseStateEligible(gates.project.state, draft.target);
  const validationError = validateReleaseDraft(
    draft,
    gates.project.code,
    gates.project.state,
    decision,
    hasApproverRole,
  );

  const resetOperation = () => {
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const selectTarget = (target: ReleaseTarget) => {
    setDraft((current) => ({
      ...current,
      target,
      targetState: "",
      acknowledged: false,
    }));
    resetOperation();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (validationError !== null) {
      return;
    }
    let key = operationKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (error) {
        setFormError(
          error instanceof Error
            ? error.message
            : "Не удалось создать ключ операции",
        );
        return;
      }
      setOperationKey(key);
    }
    mutation.mutate({
      target: draft.target,
      expectedRowVersion: gates.project.row_version,
      gateHash,
      reason: draft.reason.trim(),
      idempotencyKey: key,
    });
  };

  return (
    <div className="page release-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {gates.project.code}
        </Link>
        <span>/</span>
        <span>Допуск</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">10 · четыре глаза</p>
          <h1>Допуск расчёта к использованию</h1>
          <p>
            Решение привязано к версии проекта, фиксированному calculation
            snapshot и полному результату hard-stop-проверок. Любое изменение
            контекста делает hash недействительным.
          </p>
        </div>
        <div className="records-header__actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/APPROVALS`}
          >
            <Icon name="tasks" size={16} />
            Согласования
          </Link>
          <StatusPill value={gates.project.state} />
        </div>
      </header>

      <section className="release-targets" aria-label="Вид допуска">
        <GatePanel
          target="bid"
          decision={gates.decision}
          gateHash={gates.gate_hash}
          selected={draft.target === "bid"}
          eligible={releaseStateEligible(gates.project.state, "bid")}
          onSelect={() => selectTarget("bid")}
        />
        <GatePanel
          target="internal"
          decision={gates.internal_decision}
          gateHash={gates.internal_gate_hash}
          selected={draft.target === "internal"}
          eligible={releaseStateEligible(gates.project.state, "internal")}
          onSelect={() => selectTarget("internal")}
        />
      </section>

      <section
        className={`release-verdict ${
          decision.allowed && eligible
            ? "release-verdict--allowed"
            : "release-verdict--blocked"
        }`}
      >
        <Icon
          name={decision.allowed && eligible ? "shield" : "warning"}
          size={30}
        />
        <div>
          <p className="eyebrow">
            {releaseTargetState(draft.target)} · row version{" "}
            {gates.project.row_version}
          </p>
          <h2>
            {decision.allowed && eligible
              ? "Готово к независимому решению утверждающего"
              : "Выпуск не разрешён"}
          </h2>
          <p>
            {decision.allowed
              ? eligible
                ? "Сервер не обнаружил hard stops для зафиксированного контекста. Это не заменяет личную проверку утверждающего."
                : `Переход недоступен из состояния ${gates.project.state}.`
              : "Наличие блокирующей причины исключает утверждение, даже если итоговая арифметика сходится."}
          </p>
        </div>
      </section>

      {decision.findings.length > 0 && (
        <section className="finding-register release-findings">
          <header className="section-heading">
            <div>
              <p className="eyebrow">Полная серверная оценка</p>
              <h2>Блокирующие причины</h2>
            </div>
            <span>{decision.findings.length}</span>
          </header>
          <div className="finding-grid">
            {decision.findings.map((finding, index) => (
              <article key={`${finding.code}:${index}`}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>
                    {findingLabels[finding.code] ?? finding.message}
                  </strong>
                  <code>{finding.code}</code>
                  <p>{finding.message}</p>
                  {finding.entity_ids.length > 0 && (
                    <p>Объекты: {finding.entity_ids.join(", ")}</p>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      <FinalExpertReviewPanel
        key={draft.target}
        context={requestContext}
        project={gates.project}
        target={draft.target}
        decision={decision}
        gateHash={gateHash}
      />

      <AutomationReworkStatusPanel
        context={requestContext}
        projectId={projectId}
      />

      {mutation.isSuccess && (
        <section className="success-panel" role="status">
          <Icon name="check" size={24} />
          <div>
            <h2>Решение зафиксировано</h2>
            <p>
              Проект переведён в <strong>{mutation.data.project.state}</strong>.
              Решение и переход записаны в audit trail.
            </p>
          </div>
        </section>
      )}

      {decision.allowed && eligible && hasApproverRole ? (
        <form className="entry-form release-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Необратимое управленческое решение</p>
            <h2>Подписать допуск текущего контрольного снимка</h2>
            <p>
              Повторное использование формы после изменения документов, расчёта,
              согласований или проекта будет отклонено сервером по несовпадению
              gate hash.
            </p>
          </div>
          <label>
            Основание решения
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={4}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  reason: event.target.value,
                  acknowledged: false,
                }));
                resetOperation();
              }}
            />
          </label>
          <div className="entry-form__grid">
            <label>
              Введите шифр проекта: {gates.project.code}
              <input
                value={draft.projectCode}
                maxLength={128}
                autoComplete="off"
                onChange={(event) => {
                  setDraft((current) => ({
                    ...current,
                    projectCode: event.target.value,
                    acknowledged: false,
                  }));
                  resetOperation();
                }}
              />
            </label>
            <label>
              Введите целевое состояние: {releaseTargetState(draft.target)}
              <input
                value={draft.targetState}
                maxLength={64}
                autoComplete="off"
                onChange={(event) => {
                  setDraft((current) => ({
                    ...current,
                    targetState: event.target.value,
                    acknowledged: false,
                  }));
                  resetOperation();
                }}
              />
            </label>
          </div>
          <label className="attestation">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                resetOperation();
              }}
            />
            <span>
              Я независимо проверил полный список hard stops, доказательную
              цепочку итоговой суммы и обязательные экспертные согласования. Я
              понимаю финансовые и договорные последствия этого допуска.
            </span>
          </label>
          {(formError ?? (mutation.isError ? mutation.error.message : null)) !==
            null && (
            <p className="form-error" role="alert">
              {formError ?? (mutation.isError ? mutation.error.message : null)}
            </p>
          )}
          <button
            className="button button--primary release-submit"
            type="submit"
            disabled={mutation.isPending}
          >
            <Icon name="shield" size={17} />
            {mutation.isPending
              ? "Повторная проверка и фиксация…"
              : `Подписать ${releaseTargetState(draft.target)}`}
          </button>
        </form>
      ) : (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Команда утверждения недоступна</h2>
            <p>
              {validationError ??
                "Серверная оценка должна быть пройдена полностью."}
            </p>
          </div>
        </section>
      )}

      <div className="candidate-hash release-gate-hash">
        <span>SHA-256 текущей оценки hard stops</span>
        <code>{gateHash}</code>
      </div>
    </div>
  );
}
