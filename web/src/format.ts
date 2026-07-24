const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date);
}

export function formatMoney(
  amount: string | null,
  currency: string | null,
): string {
  if (amount === null || currency === null) {
    return "Нет подтверждённой суммы";
  }
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(amount);
  if (match === null) {
    return `${amount} ${currency}`;
  }
  const sign = match[1] === "-" ? "-" : "";
  const integer = match[2] ?? "0";
  let fraction = match[3] ?? "";
  while (fraction.length > 2 && fraction.endsWith("0")) {
    fraction = fraction.slice(0, -1);
  }
  const grouped = new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
  }).format(BigInt(integer));
  let currencyLabel = currency;
  try {
    currencyLabel =
      new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency,
        currencyDisplay: "symbol",
      })
        .formatToParts(0)
        .find((part) => part.type === "currency")?.value ?? currency;
  } catch {
    currencyLabel = currency;
  }
  return `${sign}${grouped}${fraction ? `,${fraction}` : ""} ${currencyLabel}`;
}

export function formatDecimal(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (match === null) {
    return value;
  }
  const sign = match[1] === "-" ? "-" : "";
  const integer = match[2] ?? "0";
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  const grouped = new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
  }).format(BigInt(integer));
  return `${sign}${grouped}${fraction ? `,${fraction}` : ""}`;
}

export function formatBytes(value: number): string {
  if (!Number.isSafeInteger(value) || value < 0) {
    return `${value} байт`;
  }
  const units = ["байт", "КиБ", "МиБ", "ГиБ"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: unitIndex === 0 ? 0 : 2,
  }).format(amount)} ${units[unitIndex]}`;
}

export function compactId(value: string): string {
  return value.length <= 22
    ? value
    : `${value.slice(0, 10)}…${value.slice(-8)}`;
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "Да" : "Нет";
  }
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return JSON.stringify(value);
}
