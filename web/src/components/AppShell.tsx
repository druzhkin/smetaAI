import type { ReactNode } from "react";

import { useAuth } from "../auth";
import { roleLabels } from "../labels";
import { NavLink } from "../navigation";
import type { RuntimeConfig } from "../types";
import { Icon } from "./Icon";

export function AppShell({
  config,
  children,
}: {
  config: RuntimeConfig;
  children: ReactNode;
}) {
  const auth = useAuth();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand__mark">СИ</span>
          <span>
            <strong>СметаИИ</strong>
            <small>проверяемая цена тендера</small>
          </span>
        </div>
        <nav className="primary-nav" aria-label="Основная навигация">
          <NavLink
            to="/"
            end
            aria-label="Проекты"
            className={({ isActive }) =>
              `primary-nav__item ${isActive ? "is-active" : ""}`
            }
          >
            <Icon name="portfolio" />
            <span>Проекты</span>
            <kbd>01</kbd>
          </NavLink>
          <NavLink
            to="/tasks"
            aria-label="Контроль и решения"
            className={({ isActive }) =>
              `primary-nav__item ${isActive ? "is-active" : ""}`
            }
          >
            <Icon name="tasks" />
            <span>Контроль и решения</span>
            <kbd>02</kbd>
          </NavLink>
        </nav>
        <div className="sidebar__safety">
          <Icon name="shield" />
          <div>
            <strong>Защита от ошибки</strong>
            <span>Цена скрыта, пока источники и проверки не подтверждены</span>
          </div>
        </div>
        <div className="sidebar__identity">
          <div className="identity__avatar" aria-hidden="true">
            {(auth.displayName ?? "?").slice(0, 2).toUpperCase()}
          </div>
          <div className="identity__text">
            <strong>{auth.displayName}</strong>
            <span>
              {auth.roles.length > 0
                ? auth.roles.map((role) => roleLabels[role]).join(" · ")
                : "Роли получены от IdP"}
            </span>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="Выйти"
            title="Выйти"
            onClick={() => void auth.signOut()}
          >
            <Icon name="logout" />
          </button>
        </div>
        <div className="sidebar__version">
          {config.environment} · v{config.application_version}
        </div>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}
