import {
  compactId,
  displayValue,
  formatDateTime,
  formatMoney,
} from "../format";
import { taskLabels } from "../labels";
import type { ProjectRecord } from "../types";
import { EmptyState } from "./Feedback";
import { Icon } from "./Icon";
import { StatusPill } from "./StatusPill";

const attributeLabels: Record<string, string> = {
  actor_id: "Пользователь",
  assigned_role: "Роль",
  amount_basis_id: "Основание суммы",
  calculation_run_id: "Расчёт",
  calculated_by: "Рассчитал",
  changed_by: "Изменил",
  created_by: "Создал",
  decided_by: "Решение принял",
  document_set_revision_id: "Комплект документов",
  engine_version: "Версия движка",
  event_hash: "Хеш события",
  factors: "Коэффициенты",
  finding_count: "Замечания",
  formula_hash: "Хеш формулы",
  input_hash: "Хеш входа",
  input_signature: "Подпись входа",
  line_id: "Строка BoQ",
  logical_key: "Логический ключ",
  document_type: "Тип документа",
  revision_label: "Редакция",
  issue_date: "Дата выпуска",
  critical: "Критический документ",
  corrupt: "Повреждён",
  protected: "Защищён",
  size_bytes: "Размер, байт",
  object_hash: "Хеш объекта",
  observation_ids: "Наблюдения",
  policy_version_id: "Версия политики",
  previous_hash: "Предыдущий хеш",
  quantity: "Количество",
  request_id: "ID запроса",
  rule_pack_version_id: "Версия правил",
  sequence: "Номер в цепочке",
  signing_key_id: "Ключ подписи",
  source_observation_id: "Исходное наблюдение",
  unit_rate: "Ставка",
  value: "Значение",
  wbs_node_id: "Узел WBS",
};

function recordTitle(record: ProjectRecord): string {
  if (record.kind === "APPROVAL_TASK") {
    return taskLabels[record.title] ?? record.title;
  }
  return record.title;
}

function RecordCard({ record }: { record: ProjectRecord }) {
  const attributes = Object.entries(record.attributes).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );

  return (
    <article className="record-card">
      <div className="record-card__rail">
        <span
          className={`severity-mark severity-mark--${record.severity ?? "NONE"}`}
        />
      </div>
      <div className="record-card__main">
        <div className="record-card__header">
          <div>
            <span className="record-card__kind">
              {record.kind.replaceAll("_", " ")}
            </span>
            <h3>{recordTitle(record)}</h3>
            {record.subtitle !== null && <p>{record.subtitle}</p>}
          </div>
          <div className="record-card__status">
            <StatusPill value={record.status} compact />
            <time dateTime={record.occurred_at}>
              {formatDateTime(record.occurred_at)}
            </time>
          </div>
        </div>

        {(record.amount !== null || record.unit !== null) && (
          <div className="record-card__measure">
            <strong>
              {record.currency !== null
                ? formatMoney(record.amount, record.currency)
                : (record.amount ?? "—")}
            </strong>
            {record.unit !== null && <span>за {record.unit}</span>}
          </div>
        )}

        {attributes.length > 0 && (
          <dl className="attribute-grid">
            {attributes.map(([key, value]) => (
              <div key={key}>
                <dt>{attributeLabels[key] ?? key.replaceAll("_", " ")}</dt>
                <dd title={displayValue(value)}>{displayValue(value)}</dd>
              </div>
            ))}
          </dl>
        )}

        <footer className="record-card__footer">
          <code title={record.id}>{compactId(record.id)}</code>
          {record.links.length > 0 && (
            <div className="record-links">
              <Icon name="trace" size={15} />
              {record.links.map((link) => (
                <span
                  key={`${link.relation}:${link.entity_type}:${link.entity_id}`}
                >
                  {link.relation}: {compactId(link.entity_id)}
                </span>
              ))}
            </div>
          )}
        </footer>
      </div>
    </article>
  );
}

export function RecordList({ records }: { records: ProjectRecord[] }) {
  if (records.length === 0) {
    return (
      <EmptyState
        title="Записей по фильтру нет"
        description="Это не подтверждает отсутствие работ или рисков. Проверьте комплектность источников и активные фильтры."
      />
    );
  }
  return (
    <div className="record-list">
      {records.map((record) => (
        <RecordCard key={`${record.kind}:${record.id}`} record={record} />
      ))}
    </div>
  );
}
