import { describe, expect, it } from "vitest";

import {
  mergeActualsContextPages,
  mergeForecastCandidatePages,
  resolveOperationIdentity,
  validateActualComparison,
  validateActualDecision,
  validateActualSubmission,
  validateCalibrationDecision,
  validateVarianceDecision,
} from "./actualsWorkflow";
import type {
  ActualReviewView,
  ActualsContext,
  CalibrationExampleView,
  ForecastCandidate,
  VarianceView,
} from "./types";

const attestation = {
  reason: "Сверено с первичным документом",
  projectCodeConfirmation: "P-1",
  acknowledged: true,
};

const actualReview = {
  record: {
    actual: {
      actual_id: "actual-1",
      verified: true,
    },
    is_current: true,
    task_status: "PENDING",
  },
  has_classified_variance: false,
  decision_allowed: true,
  decision_blockers: [],
} as unknown as ActualReviewView;

const variance = {
  variance_record_id: "variance-1",
  variance: { actual_id: "actual-1" },
  task_status: "PENDING",
  decision_allowed: true,
  decision_blockers: [],
} as unknown as VarianceView;

const calibration = {
  example: { example_id: "calibration-1" },
  task_status: "PENDING",
  decision_allowed: true,
  decision_blockers: [],
} as unknown as CalibrationExampleView;

const context = {
  selected_metric: "unit_rate",
  record_roles: ["PROCUREMENT"],
  evidence_candidates: [
    {
      observation: { observation_id: "observation-1" },
      observation_created_at: "2026-07-29T08:00:00Z",
      evidence_value: { metric: "unit_rate" },
      eligible: true,
      blockers: [],
    },
  ],
  records: [actualReview],
  variances: [],
  calibration_examples: [],
  next_cursor: null,
} as unknown as ActualsContext;

const forecasts = [
  {
    actual_id: "actual-1",
    forecast: { forecast_id: "forecast-1" },
  },
] as unknown as ForecastCandidate[];

describe("actuals workflow guards", () => {
  it("reuses an idempotency key only for the exact same command", () => {
    let sequence = 0;
    const createKey = () => `operation-${++sequence}`;
    const first = resolveOperationIdentity(
      null,
      { actualId: "actual-1", reason: "Reviewed" },
      createKey,
    );
    const repeated = resolveOperationIdentity(
      first,
      { actualId: "actual-1", reason: "Reviewed" },
      createKey,
    );
    const changed = resolveOperationIdentity(
      repeated,
      { actualId: "actual-1", reason: "Corrected reason" },
      createKey,
    );

    expect(repeated).toBe(first);
    expect(changed.key).not.toBe(first.key);
  });

  it("merges bounded context pages without duplicating governed records", () => {
    const merged = mergeActualsContextPages([
      { ...context, next_cursor: "page-2" },
      {
        ...context,
        records: [actualReview],
        evidence_candidates: [],
        variances: [variance],
        next_cursor: null,
      },
    ]);

    expect(merged?.records).toHaveLength(1);
    expect(merged?.variances).toEqual([variance]);
    expect(merged?.next_cursor).toBeNull();
  });

  it("keeps the newest exact release decision when forecast pages overlap", () => {
    const newest = {
      ...forecasts[0]!,
      released_by_decision_id: "release-newest",
    };
    const older = {
      ...forecasts[0]!,
      released_by_decision_id: "release-older",
    };

    expect(mergeForecastCandidatePages([[newest], [older]])).toEqual([newest]);
  });

  it("accepts only an eligible server-held evidence candidate", () => {
    expect(
      validateActualSubmission(
        { ...attestation, sourceObservationId: "observation-1" },
        context,
        "P-1",
      ),
    ).toBeNull();
    expect(
      validateActualSubmission(
        { ...attestation, sourceObservationId: "invented" },
        context,
        "P-1",
      ),
    ).toContain("серверного контекста");
    expect(
      validateActualSubmission(
        { ...attestation, sourceObservationId: "observation-1" },
        {
          ...context,
          evidence_candidates: [
            {
              ...context.evidence_candidates[0]!,
              eligible: false,
              blockers: ["SOURCE_LINEAGE_NOT_QUALIFIED"],
            },
          ],
        },
        "P-1",
      ),
    ).toContain("SOURCE_LINEAGE_NOT_QUALIFIED");
  });

  it("requires the exact released forecast and a non-default classification", () => {
    expect(
      validateActualComparison(
        {
          ...attestation,
          actualId: "actual-1",
          forecastId: "forecast-1",
          varianceReason: "PRICE_CHANGE",
          varianceReasonDetail: "Contractually evidenced supplier indexation",
        },
        context,
        forecasts,
        "P-1",
      ),
    ).toBeNull();
    expect(
      validateActualComparison(
        {
          ...attestation,
          actualId: "actual-1",
          forecastId: "invented",
          varianceReason: "PRICE_CHANGE",
          varianceReasonDetail: "Contractually evidenced supplier indexation",
        },
        context,
        forecasts,
        "P-1",
      ),
    ).toContain("выпущенного серверного снимка");
    expect(
      validateActualComparison(
        {
          ...attestation,
          actualId: "actual-1",
          forecastId: "forecast-1",
          varianceReason: "",
          varianceReasonDetail: "",
        },
        context,
        forecasts,
        "P-1",
      ),
    ).toContain("Выберите причину");
  });

  it("blocks repeated variance classification", () => {
    expect(
      validateActualComparison(
        {
          ...attestation,
          actualId: "actual-1",
          forecastId: "forecast-1",
          varianceReason: "DATA_QUALITY",
          varianceReasonDetail: "Source correction",
        },
        {
          ...context,
          records: [{ ...actualReview, has_classified_variance: true }],
          variances: [],
        },
        forecasts,
        "P-1",
      ),
    ).toContain("уже классифицировано");
  });

  it("propagates server four-eyes and integrity blockers", () => {
    expect(
      validateActualDecision(
        attestation,
        {
          ...actualReview,
          decision_allowed: false,
          decision_blockers: ["FOUR_EYES_ACTUAL_AUTHOR"],
        },
        "P-1",
      ),
    ).toContain("FOUR_EYES_ACTUAL_AUTHOR");
    expect(
      validateVarianceDecision(
        attestation,
        {
          ...variance,
          decision_allowed: false,
          decision_blockers: ["VARIANCE_INTEGRITY_FAILED"],
        },
        "P-1",
      ),
    ).toContain("VARIANCE_INTEGRITY_FAILED");
    expect(
      validateCalibrationDecision(
        attestation,
        {
          ...calibration,
          decision_allowed: false,
          decision_blockers: ["CALIBRATION_FOUR_EYES_REQUIRED"],
        },
        "P-1",
      ),
    ).toContain("CALIBRATION_FOUR_EYES_REQUIRED");
  });

  it("requires exact project attestation", () => {
    expect(
      validateCalibrationDecision(
        { ...attestation, projectCodeConfirmation: "P-2" },
        calibration,
        "P-1",
      ),
    ).toContain("точно совпадать");
  });
});
