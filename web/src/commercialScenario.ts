export type QuantityScenario = "BOQ" | "PROJECT" | "NORMALIZED";

export interface CommercialLineInput {
  lineKey: string;
  quantityBoq: string;
  quantityProject: string;
  quantityNormalized: string;
  tenderUnitPrice: string;
  fgisUnitPrice: string;
  marketUnitPrice: string;
  riskBps: number;
}

export interface CommercialAssumptions {
  tenderGrossPrice: string;
  vatBps: number;
  customerFeeBps: number;
  overheadBps: number;
  reserveBps: number;
  targetMarginBps: number;
  financingBps: number;
  tenderWeightBps: number;
  fgisWeightBps: number;
  marketWeightBps: number;
}

export interface CommercialLineResult extends CommercialLineInput {
  quantity: string;
  preliminaryUnitPriceRubles: bigint;
  directCostKopecks: bigint;
}

export interface CommercialScenarioResult {
  scenario: QuantityScenario;
  lines: CommercialLineResult[];
  directCostKopecks: bigint;
  fullCostKopecks: bigint;
  tenderGrossKopecks: bigint;
  tenderNetKopecks: bigint;
  availableAfterTermsKopecks: bigint;
  operatingResultKopecks: bigint;
  marginBps: bigint;
  requiredGrossKopecks: bigint;
  priceGapKopecks: bigint;
  verdict: "PROFIT" | "LOSS";
  status: "BLOCKED";
}

const BASIS_POINTS = 10_000n;
const RUBLES_TO_KOPECKS = 100n;
const DECIMAL_SCALE = 1_000_000_000_000_000n;

function assertBasisPoints(value: number, name: string): bigint {
  if (!Number.isSafeInteger(value) || value < 0 || value > 100_000) {
    throw new Error(`${name} must be a non-negative integer basis-point value`);
  }
  return BigInt(value);
}

function parseScaledDecimal(value: string, scaleDigits = 15): bigint {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) {
    throw new Error(`Invalid decimal value: ${value}`);
  }
  const sign = match[1] === "-" ? -1n : 1n;
  const integer = BigInt(match[2] ?? "0");
  const fraction = match[3] ?? "";
  if (fraction.length > scaleDigits) {
    throw new Error(
      `Decimal value has more than ${scaleDigits} digits: ${value}`,
    );
  }
  const scale = 10n ** BigInt(scaleDigits);
  const paddedFraction = fraction.padEnd(scaleDigits, "0");
  return sign * (integer * scale + BigInt(paddedFraction || "0"));
}

function roundHalfUp(numerator: bigint, denominator: bigint): bigint {
  if (denominator <= 0n) throw new Error("Denominator must be positive");
  const sign = numerator < 0n ? -1n : 1n;
  const absolute = numerator < 0n ? -numerator : numerator;
  const quotient = absolute / denominator;
  const remainder = absolute % denominator;
  return sign * (quotient + (remainder * 2n >= denominator ? 1n : 0n));
}

function quantityForScenario(
  line: CommercialLineInput,
  scenario: QuantityScenario,
): string {
  if (scenario === "PROJECT") return line.quantityProject;
  if (scenario === "NORMALIZED") return line.quantityNormalized;
  return line.quantityBoq;
}

export function calculateCommercialLine(
  line: CommercialLineInput,
  assumptions: CommercialAssumptions,
  scenario: QuantityScenario,
): CommercialLineResult {
  const tenderWeight = assertBasisPoints(
    assumptions.tenderWeightBps,
    "tenderWeightBps",
  );
  const fgisWeight = assertBasisPoints(
    assumptions.fgisWeightBps,
    "fgisWeightBps",
  );
  const marketWeight = assertBasisPoints(
    assumptions.marketWeightBps,
    "marketWeightBps",
  );
  if (tenderWeight + fgisWeight + marketWeight !== BASIS_POINTS) {
    throw new Error("Commercial source weights must total 10000 basis points");
  }

  const tender = parseScaledDecimal(line.tenderUnitPrice);
  const fgis = parseScaledDecimal(line.fgisUnitPrice);
  const market = parseScaledDecimal(line.marketUnitPrice);
  if (tender < 0n || fgis < 0n || market < 0n) {
    throw new Error("Commercial source prices must be non-negative");
  }
  const risk = assertBasisPoints(line.riskBps, "riskBps");
  const weightedPriceNumerator =
    tender * tenderWeight + fgis * fgisWeight + market * marketWeight;
  const preliminaryUnitPriceRubles = roundHalfUp(
    weightedPriceNumerator * (BASIS_POINTS + risk),
    BASIS_POINTS * BASIS_POINTS * DECIMAL_SCALE,
  );

  const quantity = quantityForScenario(line, scenario);
  const quantityScaled = parseScaledDecimal(quantity);
  if (quantityScaled < 0n) {
    throw new Error("Commercial quantity must be non-negative");
  }
  const directCostKopecks = roundHalfUp(
    preliminaryUnitPriceRubles * quantityScaled * RUBLES_TO_KOPECKS,
    DECIMAL_SCALE,
  );

  return {
    ...line,
    quantity,
    preliminaryUnitPriceRubles,
    directCostKopecks,
  };
}

export function calculateCommercialScenario(
  lines: CommercialLineInput[],
  assumptions: CommercialAssumptions,
  scenario: QuantityScenario,
): CommercialScenarioResult {
  const results = lines.map((line) =>
    calculateCommercialLine(line, assumptions, scenario),
  );
  const directCostKopecks = results.reduce(
    (total, line) => total + line.directCostKopecks,
    0n,
  );
  const overhead = assertBasisPoints(assumptions.overheadBps, "overheadBps");
  const reserve = assertBasisPoints(assumptions.reserveBps, "reserveBps");
  const fullCostKopecks = roundHalfUp(
    directCostKopecks * (BASIS_POINTS + overhead) * (BASIS_POINTS + reserve),
    BASIS_POINTS * BASIS_POINTS,
  );

  const tenderGrossKopecks = roundHalfUp(
    parseScaledDecimal(assumptions.tenderGrossPrice) * RUBLES_TO_KOPECKS,
    DECIMAL_SCALE,
  );
  if (tenderGrossKopecks <= 0n) {
    throw new Error("Tender gross price must be positive");
  }
  const vat = assertBasisPoints(assumptions.vatBps, "vatBps");
  const customerFee = assertBasisPoints(
    assumptions.customerFeeBps,
    "customerFeeBps",
  );
  const financing = assertBasisPoints(assumptions.financingBps, "financingBps");
  const targetMargin = assertBasisPoints(
    assumptions.targetMarginBps,
    "targetMarginBps",
  );
  const availableShare = BASIS_POINTS - customerFee - financing;
  const requiredShare = availableShare - targetMargin;
  if (availableShare <= 0n || requiredShare <= 0n) {
    throw new Error("Commercial deductions leave no positive project share");
  }

  const tenderNetKopecks = roundHalfUp(
    tenderGrossKopecks * BASIS_POINTS,
    BASIS_POINTS + vat,
  );
  const availableAfterTermsKopecks = roundHalfUp(
    tenderGrossKopecks * BASIS_POINTS * availableShare,
    (BASIS_POINTS + vat) * BASIS_POINTS,
  );
  const operatingResultKopecks = availableAfterTermsKopecks - fullCostKopecks;
  const marginBps = roundHalfUp(
    operatingResultKopecks * BASIS_POINTS,
    tenderNetKopecks,
  );
  const requiredGrossKopecks = roundHalfUp(
    fullCostKopecks * (BASIS_POINTS + vat),
    requiredShare,
  );
  const priceGapKopecks = requiredGrossKopecks - tenderGrossKopecks;

  return {
    scenario,
    lines: results,
    directCostKopecks,
    fullCostKopecks,
    tenderGrossKopecks,
    tenderNetKopecks,
    availableAfterTermsKopecks,
    operatingResultKopecks,
    marginBps,
    requiredGrossKopecks,
    priceGapKopecks,
    verdict: operatingResultKopecks >= 0n ? "PROFIT" : "LOSS",
    status: "BLOCKED",
  };
}

export function kopecksToDecimal(value: bigint): string {
  const sign = value < 0n ? "-" : "";
  const absolute = value < 0n ? -value : value;
  return `${sign}${absolute / 100n}.${(absolute % 100n).toString().padStart(2, "0")}`;
}

export function basisPointsToPercent(value: bigint): string {
  const sign = value < 0n ? "-" : "";
  const absolute = value < 0n ? -value : value;
  return `${sign}${absolute / 100n}.${(absolute % 100n).toString().padStart(2, "0")}`;
}
