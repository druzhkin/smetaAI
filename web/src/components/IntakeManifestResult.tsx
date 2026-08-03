import {
  formatIntakeFindingDetails,
  intakeFindingTitle,
  summarizeIntakeManifest,
} from "../intakeManifest";
import type { IntakeManifest } from "../types";

const MAX_DISPLAYED_FINDINGS = 100;
const severityPriority = {
  BLOCKER: 0,
  WARNING: 1,
  INFO: 2,
} as const;

export function IntakeManifestResult({
  manifest,
}: {
  manifest: IntakeManifest | null;
}) {
  if (manifest === null) {
    return (
      <section className="intake-result intake-result--blocked" role="alert">
        <div className="intake-result__header">
          <div>
            <p className="eyebrow">Результат входного контроля</p>
            <h3>Приём документов: BLOCKED</h3>
          </div>
        </div>
        <p className="intake-result__explanation">
          Обработка отмечена как завершённая, но авторитетный манифест
          отсутствует. Результат нельзя считать прошедшим входной контроль;
          требуется расследование и повторная обработка.
        </p>
      </section>
    );
  }

  const summary = summarizeIntakeManifest(manifest);
  const displayedFindings = [...manifest.findings]
    .sort(
      (left, right) =>
        severityPriority[left.severity] - severityPriority[right.severity],
    )
    .slice(0, MAX_DISPLAYED_FINDINGS);
  const findingsTruncated = displayedFindings.length < manifest.findings.length;

  return (
    <section
      className={`intake-result ${
        summary.blocked ? "intake-result--blocked" : "intake-result--passed"
      }`}
      role={summary.blocked ? "alert" : "status"}
    >
      <div className="intake-result__header">
        <div>
          <p className="eyebrow">Результат входного контроля</p>
          <h3>
            {summary.blocked
              ? "Приём документов: BLOCKED"
              : "Блокирующих ошибок входного контроля нет"}
          </h3>
        </div>
        <div className="intake-result__metrics" aria-label="Находки">
          <span>{summary.blockerCount} блокирующих</span>
          <span>{summary.warningCount} предупреждений</span>
        </div>
      </div>
      <p className="intake-result__explanation">
        {summary.blocked
          ? "Техническая обработка завершена, но комплект не прошёл входной контроль. Документальная база остаётся заблокированной до устранения находок и повторной проверки."
          : "Парсер завершил входной контроль без блокирующих находок. Это не подтверждает полноту комплекта, корректность методологии или безопасность цены предложения."}
      </p>
      {displayedFindings.length > 0 && (
        <ol className="intake-findings">
          {displayedFindings.map((finding, index) => {
            const details = formatIntakeFindingDetails(finding);
            return (
              <li
                className={`intake-finding intake-finding--${finding.severity.toLowerCase()}`}
                key={`${finding.code}-${finding.archive_path}-${index}`}
              >
                <div className="intake-finding__header">
                  <h4>{intakeFindingTitle(finding.code)}</h4>
                  <span>{finding.severity}</span>
                </div>
                <code>{finding.code}</code>
                <p>{finding.message}</p>
                <p className="intake-finding__path">{finding.archive_path}</p>
                {details.length > 0 && (
                  <ul className="intake-finding__details">
                    {details.map((detail, detailIndex) => (
                      <li key={`${detail}-${detailIndex}`}>{detail}</li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {findingsTruncated && (
        <p className="intake-result__truncation">
          Показаны {displayedFindings.length} из {manifest.findings.length}{" "}
          находок с приоритетом блокирующих. Полный перечень сохраняется в
          авторитетном манифесте записи.
        </p>
      )}
    </section>
  );
}
