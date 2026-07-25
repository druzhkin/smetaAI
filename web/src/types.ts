export type AuthenticationMode = "OIDC" | "DEVELOPMENT" | "UNAVAILABLE";

export interface RuntimeConfig {
  environment: string;
  authentication_mode: AuthenticationMode;
  oidc_authority: string | null;
  oidc_client_id: string | null;
  oidc_scope: string;
  api_base_path: string;
  application_version: string;
  max_upload_bytes: number;
}

export type ApprovalState =
  | "DRAFT"
  | "DOCUMENTS_INCOMPLETE"
  | "EXTRACTION_IN_PROGRESS"
  | "EXTRACTION_REVIEW"
  | "BOQ_IN_PROGRESS"
  | "BOQ_REVIEW"
  | "PRICING_IN_PROGRESS"
  | "RFQ_REQUIRED"
  | "CALCULATION_IN_PROGRESS"
  | "INDEPENDENT_VALIDATION"
  | "EXPERT_REVIEW"
  | "BLOCKED"
  | "APPROVED_FOR_INTERNAL_USE"
  | "APPROVED_FOR_BID"
  | "SUPERSEDED"
  | "ARCHIVED";

export type ActorRole =
  | "ESTIMATOR"
  | "PROCUREMENT"
  | "TECHNICAL_EXPERT"
  | "REVIEWER"
  | "APPROVER"
  | "METHODOLOGY_OWNER"
  | "CATALOG_OWNER"
  | "AUDITOR"
  | "ADMIN"
  | "SYSTEM";

export interface ProjectView {
  id: string;
  organization_id: string;
  code: string;
  name: string;
  state: ApprovalState;
  row_version: number;
  current_document_set_revision_id: string | null;
}

export interface DocumentSetView {
  id: string;
  project_id: string;
  manifest_hash: string;
  revision_ids: string[];
  status: string;
  created_by: string;
  created_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
}

export interface ProjectAccess {
  access_level: "MEMBER" | "OWNER";
  roles: ActorRole[];
}

export interface ProjectPortfolioItem {
  project: ProjectView;
  access: ProjectAccess;
  open_approval_count: number;
  unresolved_blocker_count: number;
  latest_total: string | null;
  latest_currency: string | null;
  updated_at: string;
}

export interface ProjectPortfolioPage {
  items: ProjectPortfolioItem[];
  next_cursor: string | null;
}

export interface WorkItem {
  task_id: string;
  project_id: string;
  project_code: string;
  project_name: string;
  task_type: string;
  entity_type: string;
  entity_id: string;
  assigned_role: ActorRole;
  status: string;
  required: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkItemPage {
  items: WorkItem[];
  next_cursor: string | null;
}

export type ApprovalDecision = "APPROVED" | "REJECTED" | "CHANGES_REQUESTED";

export interface WorkItemDecision {
  approval_id: string;
  decision: ApprovalDecision;
  decided_by: string;
  reason: string;
  evidence_ids: string[];
  related_change_ids: string[];
  decided_at: string;
}

export interface WorkItemDetail {
  item: WorkItem;
  project: ProjectView;
  policy_version_id: string | null;
  task_key: string | null;
  candidate_evidence_ids: string[];
  decisions: WorkItemDecision[];
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface EvidenceLocation {
  document_id: string;
  document_revision_id: string;
  original_object_hash: string;
  locator_kind: string;
  locator: string;
  page: number | null;
  table: string | null;
  sheet: string | null;
  cell_or_range: string | null;
}

export interface EvidenceObservation {
  observation_id: string;
  field_name: string;
  value: unknown;
  unit: string | null;
  method: string;
  method_version: string;
  source_priority: number;
  location: EvidenceLocation;
  observed_at: string;
  actor_id: string;
  confidence: string | null;
  status: string;
}

export interface ConflictObservation extends EvidenceObservation {
  adapter_qualification_id: string | null;
  adapter_qualification_status: string | null;
  adapter_qualification_valid_until: string | null;
  independence_domain: string | null;
  basis_metadata: Record<string, string>;
}

export interface EvidenceConflict {
  conflict_id: string;
  field_name: string;
  observation_ids: string[];
  reason: string;
  status: string;
  resolved_value: unknown | null;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_reason: string | null;
}

export interface ConflictReview {
  conflict: EvidenceConflict;
  conflict_updated_at: string;
  observations: ConflictObservation[];
  missing_observation_ids: string[];
  task_id: string;
  task_status: string | null;
  task_required: boolean | null;
  task_updated_at: string | null;
  task_created_by: string | null;
  resolution_allowed: boolean;
  resolution_blockers: string[];
}

export interface ConflictResolutionResult {
  conflict: EvidenceConflict;
  verified_observation: EvidenceObservation;
}

export interface ApprovalDecisionResult {
  approval_id: string;
  task_id: string;
  decision: ApprovalDecision;
  decided_by: string;
}

export type QuarantineStatus =
  | "QUARANTINED"
  | "CLEAN"
  | "REJECTED"
  | "SCAN_FAILED"
  | "PROCESSING"
  | "PROCESSED"
  | "PROCESSING_FAILED"
  | "PROCESSING_DEAD_LETTERED";

export interface QuarantinedUpload {
  upload_id: string;
  project_id: string;
  status: QuarantineStatus;
  object_hash: string;
  size_bytes: number;
  original_filename: string;
  uploaded_by: string;
  latest_scan_verdict: "CLEAN" | "INFECTED" | "ERROR" | null;
  latest_scan_report_hash: string | null;
  processed_document_id: string | null;
  processed_document_revision_id: string | null;
  candidate_document_set_revision_id: string | null;
  manifest: Record<string, unknown> | null;
  failure_code: string | null;
  processing_attempts: number;
  processing_lease_expires_at: string | null;
  processing_dead_lettered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationFinding {
  code: string;
  severity: string;
  message: string;
  entity_ids: string[];
  details: Record<string, unknown>;
}

export interface GateDecision {
  requested_state: ApprovalState;
  allowed: boolean;
  resulting_state: ApprovalState;
  findings: ValidationFinding[];
}

export interface ReleaseGateSet {
  project: ProjectView;
  decision: GateDecision;
  gate_hash: string;
  internal_decision: GateDecision;
  internal_gate_hash: string;
}

export interface ReleaseAttempt {
  project: ProjectView;
  decision: GateDecision;
}

export interface WorkbenchMetric {
  code: string;
  label: string;
  value: number;
  blocking: number;
}

export type ProjectRecordSection =
  | "DOCUMENTS"
  | "EVIDENCE"
  | "BOQ_SCOPE"
  | "PRICING"
  | "CONTRACT_RISK"
  | "CALCULATION"
  | "APPROVALS"
  | "ACTUALS"
  | "GOVERNANCE"
  | "AUDIT";

export interface ProjectRecordLink {
  relation: string;
  entity_type: string;
  entity_id: string;
}

export interface ProjectRecord {
  id: string;
  section: ProjectRecordSection;
  kind: string;
  title: string;
  subtitle: string | null;
  status: string | null;
  severity: string | null;
  current: boolean | null;
  amount: string | null;
  currency: string | null;
  unit: string | null;
  occurred_at: string;
  attributes: Record<string, unknown>;
  links: ProjectRecordLink[];
}

export interface ProjectRecordPage {
  section: ProjectRecordSection;
  items: ProjectRecord[];
  next_cursor: string | null;
}

export interface ProjectWorkbench {
  project: ProjectView;
  access: ProjectAccess;
  release_decision: GateDecision;
  metrics: WorkbenchMetric[];
  attention: ProjectRecord[];
  recent_activity: ProjectRecord[];
  latest_total: string | null;
  latest_currency: string | null;
  generated_at: string;
}

export type QuantityOperation =
  "SUM" | "PRODUCT" | "RECTANGULAR_VOLUME" | "CYLINDER_VOLUME";

export interface QuantityFormula {
  formula_id: string;
  formula_version: string;
  operation: QuantityOperation;
  inputs: Record<string, string>;
  output_unit: string;
  display_formula: string;
}

export interface QuantityDraft {
  value: string;
  unit: string;
  source_observation_ids: string[];
  source_priority: number;
  rounding_scale: number;
  waste_factor: string;
  alternative_quantity_ids: string[];
  manual_change_id: string | null;
}

export interface QuantitySubmission {
  draft: QuantityDraft;
  formula: QuantityFormula | null;
  formula_input_observation_ids: Record<string, string>;
}

export interface QuantityChangeContext {
  project_id: string;
  line_id: string;
  line_key: string;
  description: string;
  unit: string;
  current_quantity_id: string;
  current_quantity_status: string;
  current_submission: QuantitySubmission;
  document_set_revision_id: string;
  quantity_policy_version_id: string;
  quantity_formula_rules_version_id: string | null;
  manual_change_policy_version_id: string;
  critical: boolean;
  approval_role: ActorRole | null;
}

export interface QuantityManualChange {
  change_id: string;
  project_id: string;
  line_id: string;
  previous_quantity_id: string;
  critical: boolean;
  changed_by: string;
  reason: string;
  changed_at: string;
  policy_version_id: string;
  document_set_revision_id: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  approval_task_id: string | null;
  approval_task_status: string | null;
  approval_task_updated_at: string | null;
  status: string;
  applied_quantity_id: string | null;
  applied_by: string | null;
  applied_at: string | null;
}

export interface QuantityExecution {
  quantity: {
    quantity_id: string;
    value: string;
    unit: string;
    status: string;
    manual_change_id: string | null;
  };
  validation: {
    quantity_id: string;
    recalculated_value: string | null;
    passed: boolean;
    findings: ValidationFinding[];
  };
  supersedes_quantity_id: string | null;
}

export type PriceEvidenceClass =
  | "OFFICIAL_OR_PRIMARY"
  | "INDEPENDENT_MARKET"
  | "INTERNAL_HISTORY"
  | "COMMERCIAL_QUOTE";

export type VatBasis = "EXCLUSIVE" | "INCLUSIVE" | "NOT_APPLICABLE";

export interface CommercialBasis {
  currency: string;
  vat_basis: VatBasis;
  vat_rate: string | null;
  unit: string;
  package_quantity: string;
  party_quantity: string;
  region: string;
  delivery_included: boolean;
  unloading_included: boolean;
  payment_terms: string;
}

export interface PriceQuoteDraft {
  item_id: string;
  supplier_id: string | null;
  evidence_class: PriceEvidenceClass;
  source_observation_id: string;
  technical_attributes: Record<string, string>;
  amount: string;
  basis: CommercialBasis;
  quote_date: string;
  valid_until: string | null;
  lead_time_days: number | null;
  available: boolean | null;
  source_reliability: string;
}

export interface PriceQuote extends PriceQuoteDraft {
  quote_id: string;
  status: string;
}

export interface NormalizedPrice {
  normalized_price_id: string;
  quote_id: string;
  amount_per_unit: string;
  currency: string;
  unit: string;
  formula_hash: string;
  policy_version_id: string;
}

export interface PriceQuoteSummary {
  quote: PriceQuote;
  source_origin_id: string;
  normalized_prices: NormalizedPrice[];
}

export interface PriceQuoteRecord {
  quote: PriceQuote;
  source_origin_id: string;
  normalized_price_id: string | null;
}

export interface PriceDecisionSummary {
  decision_id: string;
  status: string;
  amount_per_unit: string | null;
  currency: string | null;
  unit: string | null;
  policy_version_id: string;
  derived_observation_id: string | null;
  evaluation_id: string | null;
  as_of: string | null;
  normalized_price_ids: string[];
  source_origin_ids: string[];
  approval_task_ids: string[];
  rfq_request_id: string | null;
}

export interface PriceItemContext {
  project_id: string;
  item_id: string;
  match_id: string;
  match_class: string;
  critical_price: boolean;
  required_critical_attributes: string[];
  technical_attributes: Record<string, string>;
  document_set_revision_id: string | null;
  catalog_version_id: string;
  price_policy_version_id: string;
  normalization_rounding_scale: number;
  normalization_rounding_mode: string;
  target_basis: CommercialBasis;
  normalization_references: Record<
    string,
    Record<string, Record<string, unknown>>
  >;
  quotes: PriceQuoteSummary[];
  current_decision: PriceDecisionSummary | null;
}

export interface PriceQuoteCandidate {
  project_id: string;
  item_id: string;
  source_observation_id: string;
  source_origin_id: string;
  draft: PriceQuoteDraft;
  target_basis: CommercialBasis;
  price_policy_version_id: string;
  required_reference_types: string[];
  required_adjustment_kinds: string[];
}

export interface PriceDecision {
  decision_id: string;
  item_id: string;
  status: string;
  amount_per_unit: string | null;
  currency: string | null;
  unit: string | null;
  derived_observation_id: string | null;
  triangulation: {
    item_id: string;
    quote_ids: string[];
    passed: boolean;
    resulting_status: string;
    missing_evidence_classes: PriceEvidenceClass[];
    reason: string;
  };
  relative_spread: string | null;
  approval_task_ids: string[];
  rfq_request_id: string | null;
  project_state: string;
}

export interface AppliedCalculationFactor {
  factor_id: string;
  version_id: string;
  value: string;
  evidence_or_rule_id: string;
}

export interface AtomicCostInput {
  cost_input_id: string;
  line_id: string;
  wbs_node_id: string;
  semantic_key: string;
  category: string;
  quantity: string;
  unit: string;
  unit_rate: string;
  currency: string;
  factors: AppliedCalculationFactor[];
  sign: -1 | 1;
  source_observation_id: string | null;
  approved_assumption_id: string | null;
  normative_rate_id: string | null;
  risk_reserve_id: string | null;
  derived_cost_model_id: string | null;
}

export interface CalculationPolicy {
  policy_version: string;
  currency: string;
  line_rounding_scale: number;
  total_rounding_scale: number;
  rounding_mode: string;
  independent_tolerance: string;
  expected_semantic_keys: string[];
}

export interface CalculationCandidate {
  candidate_hash: string;
  project_id: string;
  project_row_version: number;
  document_set_revision_id: string;
  calculation_model_version_id: string;
  policy: CalculationPolicy;
  inputs: AtomicCostInput[];
}

export interface FixedCalculation {
  snapshot_id: string;
  calculation_run_id: string;
  document_set_revision_id: string;
  calculation_model_version_id: string | null;
  status: string;
  currency: string | null;
  grand_total: string | null;
  independent_validation_passed: boolean | null;
  snapshot_hash: string;
  created_by: string;
  created_at: string;
  integrity_valid: boolean;
  integrity_error: string | null;
}

export interface CalculationContext {
  project: ProjectView;
  candidate: CalculationCandidate | null;
  latest_fixed_calculation: FixedCalculation | null;
  blockers: string[];
}

export interface CalculationLineResult {
  line_id: string;
  category: string;
  amount: string;
  currency: string;
}

export interface CalculationExecution {
  project: ProjectView;
  primary: {
    engine_version: string;
    currency: string;
    lines: CalculationLineResult[];
    category_totals: Record<string, string>;
    grand_total: string;
    calculated_at: string;
  };
  independent: {
    validator_version: string;
    passed: boolean;
    independently_calculated_total: string;
    primary_total: string;
    difference: string;
    tolerance: string;
    findings: ValidationFinding[];
    validated_at: string;
  };
  snapshot: {
    snapshot_id: string;
    project_id: string;
    document_set_revision_id: string;
    input_hash: string;
    output_hash: string;
    snapshot_hash: string;
    created_by: string;
    created_at: string;
    fixed: boolean;
  };
}

export interface DevelopmentIdentity {
  actorId: string;
  organizationId: string;
  roles: ActorRole[];
}
