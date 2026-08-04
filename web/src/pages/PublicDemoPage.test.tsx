import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../App";
import type { RuntimeConfig } from "../types";
import { buildPublicMatrixCsv } from "./PublicDemoPage";
import { alabugaPublicSnapshot } from "../publicSnapshot";

const config: RuntimeConfig = {
  environment: "development",
  authentication_mode: "PUBLIC_DEMO",
  oidc_authority: null,
  oidc_client_id: null,
  oidc_scope: "openid profile email",
  api_base_path: "/v1",
  application_version: "0.1.0",
  application_build_reference: null,
  max_upload_bytes: 524_288_000,
  showcase_operator_upload_enabled: true,
};

afterEach(cleanup);

describe("public project workbench", () => {
  it("shows actual Alabuga rows and source evidence without a login", () => {
    render(<App config={config} />);

    expect(
      screen.getByRole("heading", { name: "Алабуга 4527946" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Рабочий срез")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Все позиции и источники" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "Песок природный для строительных работ I класс, средний",
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("890,24").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("heading", {
        name: "Не 12 карточек: полный журнал содержит 783 ответа ФГИС ЦС",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("60 вариантов")).toBeInTheDocument();
    expect(screen.getByText("4 из 16")).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: /Открыть точный запрос/ }).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("Наименование скрыто")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Корпоративная учётная запись" }),
    ).not.toBeInTheDocument();
  });

  it("filters the project to positions with observed amounts", () => {
    render(<App config={config} />);

    fireEvent.click(screen.getByRole("button", { name: "Есть сумма" }));

    expect(screen.getByText("5 из 23")).toBeInTheDocument();
  });

  it("exports all rows and source names to a semicolon-separated CSV", () => {
    const csv = buildPublicMatrixCsv(alabugaPublicSnapshot.matrix.rows);

    expect(csv.split("\r\n")).toHaveLength(24);
    expect(csv).toContain(
      "Песок природный для строительных работ I класс, средний",
    );
    expect(csv).toContain("https://fgiscs.minstroyrf.ru/");
    expect(csv).toContain("Кабель  АПвПг 1х240/70");
    expect(csv).toContain("не сформирована");
  });
});
