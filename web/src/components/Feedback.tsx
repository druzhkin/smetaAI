import { ApiError } from "../api";
import { Icon } from "./Icon";

export function LoadingBlock({
  label = "Загрузка данных",
}: {
  label?: string;
}) {
  return (
    <div className="loading-block" role="status" aria-live="polite">
      <span className="loading-block__bar" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorBlock({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  let title = "Не удалось получить данные";
  let message =
    error instanceof Error ? error.message : "Произошла неизвестная ошибка";
  let requestId: string | null = null;

  if (error instanceof ApiError) {
    requestId = error.requestId;
    if (error.status === 403) {
      title = "Нет необходимой роли";
      message =
        "Доступ к этому контуру ограничен ролью в проекте. Финансовые данные не раскрыты.";
    } else if (error.status === 404) {
      title = "Проект недоступен";
      message =
        "Проект не найден либо скрыт информационным барьером вашей организации.";
    } else if (error.status === 401) {
      title = "Сеанс завершён";
      message = "Войдите повторно, чтобы продолжить работу.";
    }
  }

  return (
    <section className="error-block" role="alert">
      <Icon name="warning" size={24} />
      <div>
        <h2>{title}</h2>
        <p>{message}</p>
        {requestId !== null && (
          <p className="error-block__reference">ID запроса: {requestId}</p>
        )}
      </div>
      {onRetry !== undefined && (
        <button
          className="button button--secondary"
          type="button"
          onClick={onRetry}
        >
          <Icon name="refresh" size={16} />
          Повторить
        </button>
      )}
    </section>
  );
}

export function EmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__rule" />
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}
