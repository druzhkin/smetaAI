import type { RuntimeConfig } from "./types";

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch("/v1/runtime-config", {
    credentials: "omit",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      `Не удалось загрузить конфигурацию приложения (${response.status})`,
    );
  }
  return (await response.json()) as RuntimeConfig;
}
