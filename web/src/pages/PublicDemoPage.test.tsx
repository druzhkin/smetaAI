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
  it("shows the Alabuga VOR, preliminary economics and source evidence without a login", () => {
    render(<App config={config} />);

    expect(
      screen.getByRole("heading", { name: "Алабуга 4527946" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Рабочий срез")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "ВОР · себестоимость проекта" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "При текущей цене тендер убыточен" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/5.368.605,77/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/-2.391.884,77/).length).toBeGreaterThan(0);
    expect(screen.getByText("маржа -62,27%")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "По ВОР заказчика" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.queryByRole("button", { name: "Нормализовано" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("23 из 23")).toBeInTheDocument();
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
    expect(
      screen.getByText("Формула книги исправлена, данные не утверждены"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Цена системы, руб./ед." }),
    ).toBeInTheDocument();
  });

  it("filters the project to positions with observed amounts", () => {
    render(<App config={config} />);

    expect(screen.getByText("23 из 23")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Цена ФГИС 2" }));
    expect(screen.getByText("2 из 23")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "С суммами 5" }));
    expect(screen.getByText("5 из 23")).toBeInTheDocument();
  });

  it("explains that work rows require normative calculation instead of FGIS resource prices", () => {
    render(<App config={config} />);

    expect(
      screen.getAllByText("Работа · требуется ГЭСН").length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("ФГИС 0")).not.toBeInTheDocument();
  });

  it("switches the quantities and recalculates the tender result", () => {
    render(<App config={config} />);

    fireEvent.click(
      screen.getByRole("button", { name: "По проектной документации" }),
    );

    expect(screen.getAllByText(/6.721.619,61/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/-3.744.898,61/).length).toBeGreaterThan(0);
    expect(screen.getByText("маржа -97,5%")).toBeInTheDocument();
  });

  it("exports all rows and source names to a semicolon-separated CSV", () => {
    const csv = buildPublicMatrixCsv(alabugaPublicSnapshot.matrix.rows);

    expect(csv.split("\r\n")).toHaveLength(24);
    expect(csv).toContain(
      "Песок природный для строительных работ I класс, средний",
    );
    expect(csv).toContain("https://fgiscs.minstroyrf.ru/");
    expect(csv).toContain("Кабель  АПвПг 1х240/70");
    expect(csv).toContain('"Предварительная цена системы, руб./ед. без НДС"');
    expect(csv).toContain('"6149";"73788.00"');
    expect(csv).toContain('"BLOCKED"');
  });
});
