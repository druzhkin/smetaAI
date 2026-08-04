import { Icon } from "../components/Icon";
import type { RuntimeConfig } from "../types";

const stages = [
  {
    number: "01",
    title: "Документы",
    detail: "ВОР прочитана, строки и исходные характеристики извлечены.",
    state: "Готово",
    tone: "done",
  },
  {
    number: "02",
    title: "Сопоставление",
    detail:
      "Названия сравниваются с источниками по характеристикам и единицам.",
    state: "В работе",
    tone: "active",
  },
  {
    number: "03",
    title: "Источники цен",
    detail: "Найдены кандидаты ФГИС ЦС и рынка, но они ещё не подтверждены.",
    state: "Кандидаты",
    tone: "active",
  },
  {
    number: "04",
    title: "Нормализация",
    detail: "Единицы, НДС, дата, регион и логистика должны быть доказаны.",
    state: "Ожидает",
    tone: "waiting",
  },
  {
    number: "05",
    title: "Расчёт",
    detail: "Цена не раскрывается, пока обязательные проверки не пройдены.",
    state: "BLOCKED",
    tone: "blocked",
  },
  {
    number: "06",
    title: "Финальный эксперт",
    detail:
      "Вмешивается только после расчёта: принимает или возвращает строки.",
    state: "Не начат",
    tone: "waiting",
  },
] as const;

const moduleFlow = [
  ["01", "Импорт", "XLSX, PDF, DOCX и архивы"],
  ["02", "Разбор ВОР", "Строки, объёмы и характеристики"],
  ["03", "Сопоставление", "Наименование ВОР ↔ наименование источника"],
  ["04", "Источники", "ФГИС ЦС, тендеры и открытый рынок"],
  ["05", "Расчёт", "Нормализация, проверки и объяснение"],
  ["06", "Эксперт", "Только финальное принятие или возврат"],
] as const;

const matrixRows = [
  { id: "XLSX-ROW-19", fgis: 5, market: 16 },
  { id: "XLSX-ROW-21", fgis: 2, market: 2 },
  { id: "ROW-16", fgis: 1, market: 0 },
  { id: "ROW-19", fgis: 2, market: 2 },
  { id: "ROW-21", fgis: 0, market: 1 },
  { id: "ROW-23", fgis: 2, market: 1 },
] as const;

function SourceResult({
  count,
  kind,
}: {
  count: number;
  kind: "ФГИС ЦС" | "рынка";
}) {
  if (count === 0) {
    return (
      <div className="public-matrix__empty">
        <strong>Нет кандидатов</strong>
        <span>Подтверждённая цена отсутствует</span>
      </div>
    );
  }
  const lastTwoDigits = count % 100;
  const candidateEnding =
    lastTwoDigits >= 11 && lastTwoDigits <= 14
      ? "исследовательских кандидатов"
      : count % 10 === 1
        ? "исследовательский кандидат"
        : count % 10 >= 2 && count % 10 <= 4
          ? "исследовательских кандидата"
          : "исследовательских кандидатов";
  return (
    <div className="public-matrix__candidate">
      <strong>
        {count} {candidateEnding}
      </strong>
      <span>Наименования {kind} видны в рабочем контуре</span>
      <small>Цена не используется до проверки соответствия</small>
    </div>
  );
}

export function PublicDemoPage({ config }: { config: RuntimeConfig }) {
  return (
    <main className="public-demo">
      <header className="public-demo__topbar">
        <a
          className="public-demo__brand"
          href="#overview"
          aria-label="СметаИИ — к обзору"
        >
          <span>СИ</span>
          <strong>СметаИИ</strong>
        </a>
        <nav aria-label="Разделы публичного обзора">
          <a href="#overview">Обзор</a>
          <a href="#architecture">Как работает</a>
          <a href="#matrix">Ценовая матрица</a>
        </nav>
        <div className="public-demo__mode">
          <Icon name="shield" size={16} />
          <span>Только чтение</span>
        </div>
      </header>

      <section className="public-hero" id="overview">
        <div className="public-hero__copy">
          <p className="eyebrow">Текущий диагностический срез · 04.08.2026</p>
          <h1>
            Алабуга <span>4527946</span>
          </h1>
          <p className="public-hero__lead">
            Система прочитала ВОР и собрала исследовательские кандидаты цен.
            Итоговая стоимость пока не рассчитана: ни одна строка не прошла
            полный набор обязательных проверок.
          </p>
          <div className="public-hero__actions">
            <a className="button button--public" href="#matrix">
              Смотреть ценовую матрицу
              <Icon name="arrow" />
            </a>
            <span>
              Коммерческие значения и документы скрыты в публичном режиме
            </span>
          </div>
        </div>
        <aside
          className="public-hero__decision"
          aria-label="Текущий статус проекта"
        >
          <div className="public-hero__decision-label">
            <span>Решение системы</span>
            <Icon name="warning" size={20} />
          </div>
          <strong>BLOCKED</strong>
          <p>Безопасная цена не выпущена</p>
          <dl>
            <div>
              <dt>Позиции ВОР</dt>
              <dd>23</dd>
            </div>
            <div>
              <dt>Готовы к выпуску</dt>
              <dd>0</dd>
            </div>
          </dl>
        </aside>
      </section>

      <section className="public-stats" aria-label="Сводка обработки">
        <article>
          <span>01 / ВОР</span>
          <strong>23</strong>
          <p>строки извлечены из исходного файла</p>
        </article>
        <article>
          <span>02 / ФГИС ЦС</span>
          <strong>12</strong>
          <p>кандидатов найдено, подтверждённых цен — 0</p>
        </article>
        <article>
          <span>03 / Рынок</span>
          <strong>22</strong>
          <p>кандидата найдено, нормализованных цен — 0</p>
        </article>
        <article className="is-blocked">
          <span>04 / Результат</span>
          <strong>Не выпущен</strong>
          <p>система запретила показывать неподтверждённую сумму как расчёт</p>
        </article>
      </section>

      <section className="public-section" id="architecture">
        <header className="public-section__heading">
          <div>
            <p className="eyebrow">Как работает система</p>
            <h2>Один поток от документа до финального решения</h2>
          </div>
          <p>
            Автоматика выполняет механическую работу сама. Эксперт подключается
            только в самом конце и видит готовый результат вместе с причинами
            блокировок.
          </p>
        </header>

        <div className="public-module-flow" aria-label="Архитектура модулей">
          {moduleFlow.map(([number, title, detail], index) => (
            <article key={number}>
              <span>{number}</span>
              <div>
                <strong>{title}</strong>
                <p>{detail}</p>
              </div>
              {index < moduleFlow.length - 1 && <Icon name="arrow" size={17} />}
            </article>
          ))}
        </div>

        <div className="public-stage-grid">
          {stages.map((stage) => (
            <article
              className={`public-stage is-${stage.tone}`}
              key={stage.number}
            >
              <header>
                <span>{stage.number}</span>
                <em>{stage.state}</em>
              </header>
              <h3>{stage.title}</h3>
              <p>{stage.detail}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="public-section public-section--matrix" id="matrix">
        <header className="public-section__heading">
          <div>
            <p className="eyebrow">Проверяемое сопоставление</p>
            <h2>Ценовая матрица по каждой строке ВОР</h2>
          </div>
          <p>
            В рабочем контуре рядом показываются исходное и найденное
            наименования, характеристики, единицы, ссылка и снимок источника.
            Здесь содержимое строк скрыто, но логика решения сохранена.
          </p>
        </header>

        <div className="public-matrix-summary">
          <span>Показано 6 из 23 строк</span>
          <strong>Все 23 строки заблокированы</strong>
          <span>Цены и исходные документы не публикуются</span>
        </div>

        <div className="public-matrix-scroll">
          <table className="public-matrix">
            <thead>
              <tr>
                <th>Позиция ВОР</th>
                <th>Выигранные тендеры</th>
                <th>ФГИС ЦС</th>
                <th>Рынок</th>
                <th>Предложение системы</th>
              </tr>
            </thead>
            <tbody>
              {matrixRows.map((row) => (
                <tr key={row.id}>
                  <th scope="row">
                    <code>{row.id}</code>
                    <strong>Наименование скрыто</strong>
                    <span>
                      Характеристики и количество скрыты в публичном режиме
                    </span>
                  </th>
                  <td>
                    <div className="public-matrix__empty">
                      <strong>Нет подтверждённых данных</strong>
                      <span>Цена ранее выигранного тендера отсутствует</span>
                    </div>
                  </td>
                  <td>
                    <SourceResult count={row.fgis} kind="ФГИС ЦС" />
                  </td>
                  <td>
                    <SourceResult count={row.market} kind="рынка" />
                  </td>
                  <td>
                    <div className="public-matrix__blocked">
                      <span>BLOCKED</span>
                      <strong>Цена не предлагается</strong>
                      <p>
                        Недостаточно подтверждённых и нормализованных
                        источников.
                      </p>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="public-blockers">
          <div>
            <Icon name="warning" size={22} />
            <span>Почему расчёт остановлен</span>
          </div>
          <ul>
            <li>
              не доказано точное соответствие наименований и характеристик;
            </li>
            <li>
              кандидаты цен ещё не приведены к одной единице и коммерческой
              базе;
            </li>
            <li>
              нет подтверждённой истории выигранных тендеров по этим позициям;
            </li>
            <li>
              отсутствует нормативный расчёт утверждённого сметного движка.
            </li>
          </ul>
        </div>
      </section>

      <footer className="public-demo__footer">
        <div>
          <strong>СметаИИ</strong>
          <span>Публичный обзор без доступа к проектным данным</span>
        </div>
        <p>
          Этот экран не является сметой, коммерческим предложением или
          подтверждением безопасной цены. Рабочий API и операции выпуска
          остаются защищёнными.
        </p>
        <code>
          v{config.application_version} · {config.environment}
        </code>
      </footer>
    </main>
  );
}
