import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { createProject, newIdempotencyKey, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../components/Icon";
import { validateProjectDraft, type ProjectDraft } from "../intake";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

const initialDraft: ProjectDraft = {
  code: "",
  name: "",
  reason: "",
  acknowledged: false,
};

export function ProjectCreatePage({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<ProjectDraft>(initialDraft);
  const [formError, setFormError] = useState<string | null>(null);
  const [operationKey, setOperationKey] = useState<string | null>(null);
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const mutation = useMutation({
    mutationFn: (input: {
      code: string;
      name: string;
      reason: string;
      idempotencyKey: string;
    }) => createProject(context, input),
    onSuccess: async (project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${encodeURIComponent(project.id)}`, {
        replace: true,
      });
    },
  });
  const canCreate = auth.roles.includes("ESTIMATOR");
  const validationError = validateProjectDraft(draft);

  const change = (patch: Partial<ProjectDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canCreate) {
      setFormError("Создать проект может только пользователь с ролью сметчика");
      return;
    }
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
      code: draft.code.trim(),
      name: draft.name.trim(),
      reason: draft.reason.trim(),
      idempotencyKey: key,
    });
  };

  return (
    <div className="page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <span>Новый проект</span>
      </nav>
      <header className="entry-header">
        <div>
          <p className="eyebrow">Регистрация конкурсной процедуры</p>
          <h1>Создать проект</h1>
          <p>
            Проект создаётся в состоянии DRAFT. Ни одна цена и ни один вывод о
            комплектности на этом шаге не формируются.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Создатель становится владельцем проекта. Организация берётся из
            проверенной identity-сессии.
          </span>
        </div>
      </header>

      {!canCreate ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Создание проекта недоступно</h2>
            <p>
              Требуется роль сметчика. Изменение ролей выполняется вне этой
              формы.
            </p>
          </div>
        </section>
      ) : (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Идентификация</p>
            <h2>Паспорт регистрации</h2>
            <p>
              Шифр уникален в пределах организации и после создания используется
              во всех маршрутах согласования и аудита.
            </p>
          </div>

          <div className="entry-form__grid">
            <label className="decision-field">
              <span>Шифр проекта</span>
              <input
                type="text"
                value={draft.code}
                maxLength={128}
                autoComplete="off"
                spellCheck={false}
                required
                onChange={(event) => change({ code: event.target.value })}
                placeholder="СМ-2026-041"
              />
              <small>Без пробелов; уникален в организации.</small>
            </label>
            <label className="decision-field">
              <span>Наименование объекта</span>
              <input
                type="text"
                value={draft.name}
                maxLength={500}
                required
                onChange={(event) => change({ name: event.target.value })}
                placeholder="Реконструкция водовода Ду 600"
              />
              <small>Как указано в актуальной конкурсной документации.</small>
            </label>
          </div>

          <label className="decision-field">
            <span>Основание создания</span>
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={5}
              required
              onChange={(event) => change({ reason: event.target.value })}
              placeholder="Укажите источник получения комплекта, дату и ответственного."
            />
            <small>{draft.reason.length} / 2000</small>
          </label>

          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
            <span>
              Я сверил шифр, наименование и организацию конкурсного проекта.
              Создание не означает полноту документации или готовность расчёта.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось создать проект")}
              </span>
            </div>
          )}

          <div className="decision-form__actions">
            <Link className="button button--secondary" to="/">
              Отмена
            </Link>
            <button
              className="button button--primary"
              type="submit"
              disabled={mutation.isPending}
            >
              {mutation.isPending ? "Создание…" : "Создать проект в DRAFT"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
