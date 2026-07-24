import { useState, type FormEvent } from "react";

import { useAuth } from "../auth";
import { roleLabels } from "../labels";
import type { ActorRole } from "../types";
import { Icon } from "../components/Icon";

const selectableRoles: ActorRole[] = [
  "ESTIMATOR",
  "PROCUREMENT",
  "TECHNICAL_EXPERT",
  "REVIEWER",
  "APPROVER",
  "METHODOLOGY_OWNER",
  "CATALOG_OWNER",
  "AUDITOR",
  "ADMIN",
];

export function LoginPage() {
  const auth = useAuth();
  const [actorId, setActorId] = useState("operator");
  const [organizationId, setOrganizationId] = useState("org-1");
  const [roles, setRoles] = useState<ActorRole[]>(["ESTIMATOR", "REVIEWER"]);

  function submitDevelopmentIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!actorId.trim() || !organizationId.trim() || roles.length === 0) {
      return;
    }
    auth.setDevelopmentIdentity({ actorId, organizationId, roles });
  }

  return (
    <main className="login-page">
      <section className="login-intro">
        <div className="login-brand">
          <span>TG</span>
          <strong>TenderGuard</strong>
        </div>
        <p className="eyebrow">Промышленный контур расчёта</p>
        <h1>Цена выпускается только вместе с доказательствами.</h1>
        <p>
          Документы, объёмы, нормы, коммерческие условия и согласования связаны
          в одну воспроизводимую цепочку. Неразрешённый риск блокирует выпуск.
        </p>
        <ol className="login-principles">
          <li>
            <span>01</span>
            Независимое извлечение и пересчёт
          </li>
          <li>
            <span>02</span>
            Четыре глаза для критических решений
          </li>
          <li>
            <span>03</span>
            Полный audit trail до исходной ячейки
          </li>
        </ol>
      </section>

      <section className="login-panel" aria-labelledby="login-title">
        <Icon name="shield" size={30} />
        <p className="eyebrow">Защищённый вход</p>
        <h2 id="login-title">
          {auth.mode === "DEVELOPMENT"
            ? "Изолированный режим разработки"
            : "Корпоративная учётная запись"}
        </h2>

        {auth.mode === "OIDC" && (
          <>
            <p>
              Роли и организация будут получены от корпоративного провайдера
              идентификации.
            </p>
            <button
              className="button button--primary button--wide"
              type="button"
              onClick={() => void auth.signIn()}
            >
              Войти через SSO
              <Icon name="arrow" />
            </button>
          </>
        )}

        {auth.mode === "DEVELOPMENT" && (
          <form onSubmit={submitDevelopmentIdentity} className="dev-login-form">
            <div className="development-warning">
              <Icon name="warning" size={18} />
              Эти заголовки допустимы только на изолированной рабочей станции.
              Staging и production такой режим не запустят.
            </div>
            <label>
              Пользователь
              <input
                value={actorId}
                maxLength={128}
                required
                autoComplete="off"
                onChange={(event) => setActorId(event.target.value)}
              />
            </label>
            <label>
              Организация
              <input
                value={organizationId}
                maxLength={64}
                required
                autoComplete="off"
                onChange={(event) => setOrganizationId(event.target.value)}
              />
            </label>
            <fieldset>
              <legend>Роли для локальной проверки</legend>
              <div className="role-grid">
                {selectableRoles.map((role) => (
                  <label key={role} className="check-option">
                    <input
                      type="checkbox"
                      checked={roles.includes(role)}
                      onChange={(event) => {
                        setRoles((current) =>
                          event.target.checked
                            ? [...current, role]
                            : current.filter((item) => item !== role),
                        );
                      }}
                    />
                    <span>{roleLabels[role]}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <button
              className="button button--primary button--wide"
              type="submit"
              disabled={
                !actorId.trim() || !organizationId.trim() || roles.length === 0
              }
            >
              Открыть рабочий контур
              <Icon name="arrow" />
            </button>
          </form>
        )}

        {auth.mode === "UNAVAILABLE" && (
          <div className="development-warning">
            <Icon name="warning" size={18} />
            Аутентификация не настроена. Работа с проектными данными
            заблокирована.
          </div>
        )}

        {auth.error !== null && <p className="form-error">{auth.error}</p>}
      </section>
    </main>
  );
}
