import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { loadRuntimeConfig } from "./runtime";
import "./styles.css";

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("Root element is missing");
}
const root = createRoot(rootElement);

void loadRuntimeConfig()
  .then((config) => {
    root.render(
      <StrictMode>
        <App config={config} />
      </StrictMode>,
    );
  })
  .catch((error: unknown) => {
    const message =
      error instanceof Error
        ? error.message
        : "Конфигурация приложения недоступна";
    root.render(
      <main className="fatal-screen" role="alert">
        <div>
          <p>TenderGuard</p>
          <h1>Запуск заблокирован</h1>
          <span>{message}</span>
        </div>
      </main>,
    );
  });
