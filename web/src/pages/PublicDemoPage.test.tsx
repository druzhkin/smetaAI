import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import type { RuntimeConfig } from "../types";

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
};

describe("public demo mode", () => {
  it("opens the read-only project overview without rendering a login", () => {
    render(<App config={config} />);

    expect(
      screen.getByRole("heading", { name: "Алабуга 4527946" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Только чтение")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "Ценовая матрица по каждой строке ВОР",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Все 23 строки заблокированы")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Корпоративная учётная запись" }),
    ).not.toBeInTheDocument();
  });
});
