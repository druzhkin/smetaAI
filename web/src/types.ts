export type AuthenticationMode = "OIDC" | "DEVELOPMENT" | "UNAVAILABLE";

export interface RuntimeConfig {
  environment: string;
  authentication_mode: AuthenticationMode;
  oidc_authority: string | null;
  oidc_client_id: string | null;
  oidc_scope: string;
  api_base_path: string;
  application_version: string;
  application_build_reference: string | null;
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

export interface PassportFact {
  fact_id: string;
  field_name: string;
  value: unknown;
  unit: string | null;
  observation_ids: string[];
  independence_source_ids: string[];
  status: string;
  supersedes_fact_id: string | null;
  is_current: boolean;
  created_by: string;
  verified_by: string | null;
  reviewed_by: string | null;
  requirements_version_id: string;
  document_set_revision_id: string;
  approval_task_id: string;
  updated_at: string;
}

export interface PassportEvidenceCandidate {
  observation: EvidenceObservation;
  adapter_qualification_id: string | null;
  adapter_status: string | null;
  adapter_valid_until: string | null;
  independence_domain: string | null;
  eligible: boolean;
  blockers: string[];
}

export interface PassportValidation {
  passport: {
    project_id: string;
    facts: Array<{
      field_name: string;
      value: unknown;
      unit: string | null;
      observation_ids: string[];
      independence_source_ids: string[];
      status: string;
    }>;
    passport_version: string;
  };
  findings: ValidationFinding[];
  requirements_version_id: string;
}

export interface PassportFactReview {
  fact: PassportFact;
  task_status: string;
  task_updated_at: string;
  assigned_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface PassportContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  requirements_version_id: string;
  requirements_content_hash: string;
  required_fields: string[];
  independently_verified_fields: string[];
  optional_fields: string[];
  review_role: ActorRole;
  selected_field_name: string;
  facts: PassportFactReview[];
  evidence_candidates: PassportEvidenceCandidate[];
  candidates_truncated: boolean;
  validation: PassportValidation;
  unresolved_conflict_ids: string[];
}

export interface PassportDecisionResult {
  fact: PassportFact;
  validation: PassportValidation;
  approval_id: string;
  decision: ApprovalDecision;
}

export type ContractTermKind =
  | "COMPLETION_DATES"
  | "PHASING"
  | "PENALTIES"
  | "RETENTION"
  | "BID_OR_PERFORMANCE_SECURITY"
  | "BANK_GUARANTEE"
  | "ADVANCE"
  | "PAYMENT_DEFERRAL"
  | "WARRANTY"
  | "FIXED_PRICE"
  | "CURRENCY_RISK"
  | "DESIGN_ERROR_LIABILITY"
  | "MOBILISATION"
  | "DOCUMENTATION"
  | "INDEXATION_LIMITS"
  | "ACCEPTANCE_PROCEDURE";

export interface ContractCostImpact {
  amount: string;
  currency: string | null;
  cost_component_line_id: string | null;
  cost_component_semantic_key: string | null;
  derived_cost_model_id: string | null;
  no_cost_reason: string | null;
}

export interface ContractTerm {
  term_id: string;
  kind: ContractTermKind;
  value: string;
  observation_ids: string[];
  independence_source_ids: string[];
  verified: boolean;
  cost_impact_resolved: boolean;
  supersedes_term_id: string | null;
  is_current: boolean;
  created_by: string;
  verified_by: string | null;
  rules_version_id: string;
  document_set_revision_id: string;
  approval_task_id: string;
  updated_at: string;
  approval_task_ids: string[];
  cost_impact_proposal: ContractCostImpact | null;
  cost_impact_task_statuses: Record<string, string>;
  cost_impact_approved_by: string | null;
  cost_impact_finalized_at: string | null;
}

export interface ContractEvidenceCandidate {
  observation: EvidenceObservation;
  adapter_qualification_id: string | null;
  adapter_status: string | null;
  adapter_valid_until: string | null;
  independence_domain: string | null;
  eligible: boolean;
  blockers: string[];
}

export interface ContractTermReview {
  term: ContractTerm;
  task_status: string;
  task_updated_at: string;
  assigned_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface ContractImpactCandidate {
  derived_cost_model_id: string;
  amount: string;
  currency: string;
  cost_component_line_id: string;
  cost_component_semantic_key: string;
  eligible: boolean;
  blockers: string[];
}

export interface ContractValidation {
  assessment: {
    assessment_version: string;
    terms: Array<{
      term_id: string;
      kind: ContractTermKind;
      value: string;
      observation_ids: string[];
      independence_source_ids: string[];
      verified: boolean;
      cost_impact_resolved: boolean;
      cost_impact_amount: string | null;
      cost_impact_currency: string | null;
      cost_input_id: string | null;
      approved_assumption_id: string | null;
      derived_cost_model_id: string | null;
    }>;
    required_term_kinds: ContractTermKind[];
  };
  findings: ValidationFinding[];
  rules_version_id: string;
}

export interface ContractContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  rules_version_id: string;
  rules_content_hash: string;
  required_term_kinds: ContractTermKind[];
  independently_verified_term_kinds: ContractTermKind[];
  evidence_field_names: Partial<Record<ContractTermKind, string>>;
  review_role: ActorRole;
  selected_kind: ContractTermKind;
  terms: ContractTermReview[];
  evidence_candidates: ContractEvidenceCandidate[];
  impact_candidates: ContractImpactCandidate[];
  candidates_truncated: boolean;
  validation: ContractValidation;
  unresolved_conflict_ids: string[];
}

export interface ContractDecisionResult {
  term: ContractTerm;
  validation: ContractValidation;
  approval_id: string;
  decision: ApprovalDecision;
}

export interface RiskDraft {
  risk_key: string;
  description: string;
  probability: string;
  impact_min: string;
  impact_most_likely: string;
  impact_max: string;
  currency: string;
  observation_ids: string[];
  correlated: boolean;
  correlation_group: string | null;
  mitigation_cost_input_id: string | null;
}

export interface RiskItem {
  row_id: string;
  risk_key: string;
  risk: {
    risk_id: string;
    description: string;
    probability: string;
    impact_min: string;
    impact_most_likely: string;
    impact_max: string;
    currency: string;
    observation_ids: string[];
    status: string;
    correlated: boolean;
    correlation_group: string | null;
    mitigation_cost_input_id: string | null;
  };
  independence_source_ids: string[];
  supersedes_risk_id: string | null;
  is_current: boolean;
  created_by: string;
  verified_by: string | null;
  risk_model_version_id: string;
  risk_model_content_hash: string;
  document_set_revision_id: string;
  approval_task_id: string;
  updated_at: string;
}

export interface RiskEvidenceCandidate {
  observation: EvidenceObservation;
  draft: RiskDraft | null;
  adapter_qualification_id: string | null;
  adapter_status: string | null;
  adapter_valid_until: string | null;
  independence_domain: string | null;
  eligible: boolean;
  blockers: string[];
}

export interface RiskItemReview {
  item: RiskItem;
  task_status: string;
  task_updated_at: string;
  assigned_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface RiskCalculation {
  calculation_id: string;
  calculation: {
    policy_version: string;
    expected_reserve: string;
    currency: string;
    per_risk_expected_impact: Record<string, string>;
    findings: ValidationFinding[];
    passed: boolean;
  };
  status: string;
  input_signature: string;
  output_hash: string;
  independent_validation_passed: boolean;
  risk_item_ids: string[];
  document_set_revision_id: string;
  supersedes_calculation_id: string | null;
  created_at: string;
}

export interface RiskContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  risk_model_version_id: string;
  risk_model_content_hash: string;
  risk_keys: string[];
  required_risk_keys: string[];
  independently_verified_risk_keys: string[];
  evidence_field_names: Record<string, string>;
  review_role: ActorRole;
  minimum_risk_items: number;
  selected_risk_key: string;
  items: RiskItemReview[];
  evidence_candidates: RiskEvidenceCandidate[];
  candidates_truncated: boolean;
  current_calculation: RiskCalculation | null;
  calculation_blockers: string[];
  unresolved_conflict_ids: string[];
}

export interface RiskDecisionResult {
  item: RiskItem;
  approval_id: string;
  decision: "APPROVED" | "REJECTED";
}

export type ActualSourceClass =
  | "ACCEPTANCE_CERTIFICATE"
  | "SUPPLIER_INVOICE"
  | "ERP_POSTING"
  | "FINANCIAL_LEDGER"
  | "TIMESHEET"
  | "LOGISTICS_DOCUMENT"
  | "RISK_REGISTER"
  | "AS_BUILT_MEASUREMENT"
  | "OTHER_CONTROLLED";

export type ForecastBasis =
  | "ATOMIC_QUANTITY"
  | "ATOMIC_UNIT_RATE"
  | "ATOMIC_AMOUNT"
  | "PROJECT_COST_TOTAL";

export type VarianceReason =
  | "SCOPE_CHANGE"
  | "QUANTITY_ERROR"
  | "PRICE_CHANGE"
  | "SUPPLIER_CHANGE"
  | "PRODUCTIVITY_VARIANCE"
  | "LOGISTICS_VARIANCE"
  | "SCHEDULE_VARIANCE"
  | "RISK_REALISED"
  | "DATA_QUALITY"
  | "METHODOLOGY_ERROR"
  | "OTHER_APPROVED";

export interface ActualEvidenceValue {
  actual_key: string;
  entity_type: string;
  entity_id: string;
  metric: string;
  value: string;
  unit: string;
  source_class: ActualSourceClass;
  occurred_on: string;
}

export interface ActualMetricDefinition {
  metric: string;
  entity_type: string;
  evidence_field_name: string;
  forecast_basis: ForecastBasis;
  allowed_units: string[];
  allowed_source_classes: ActualSourceClass[];
}

export interface ActualFact {
  actual_id: string;
  project_id: string;
  entity_id: string;
  metric: string;
  value: string;
  unit: string;
  occurred_on: string;
  source_observation_id: string;
  verified: boolean;
  verified_by: string | null;
  actual_key: string | null;
  source_class: ActualSourceClass | null;
  status: string;
}

export interface ActualRecordView {
  actual: ActualFact;
  actual_key: string;
  supersedes_actual_id: string | null;
  is_current: boolean;
  created_by: string;
  policy_version_id: string;
  policy_content_hash: string;
  source_leaf_ids: string[];
  project_outcome_evidence_ids: string[];
  approval_task_id: string;
  task_status: string;
  task_updated_at: string;
  created_at: string;
}

export interface ActualReviewView {
  record: ActualRecordView;
  assigned_role: ActorRole;
  has_classified_variance: boolean;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface ActualEvidenceCandidate {
  observation: EvidenceObservation;
  observation_created_at: string;
  evidence_value: ActualEvidenceValue | null;
  eligible: boolean;
  blockers: string[];
}

export interface ForecastFact {
  forecast_id: string;
  project_id: string;
  entity_id: string;
  metric: string;
  value: string;
  unit: string;
  snapshot_id: string;
  snapshot_hash: string | null;
  cost_input_row_id: string | null;
  forecast_basis: ForecastBasis | null;
}

export interface ForecastCandidate {
  actual_id: string;
  forecast: ForecastFact;
  released_by_decision_id: string;
}

export interface ForecastCandidatePage {
  items: ForecastCandidate[];
  next_cursor: string | null;
}

export interface VarianceRecord {
  forecast_id: string;
  actual_id: string;
  absolute_variance: string;
  relative_variance: string | null;
  reason: VarianceReason;
  reason_detail: string;
  classified_by: string;
  status: string;
  reviewed_by: string | null;
}

export interface VarianceView {
  variance_record_id: string;
  variance: VarianceRecord;
  forecast: ForecastFact;
  policy_version_id: string;
  policy_content_hash: string;
  approval_task_id: string;
  task_status: string;
  task_updated_at: string;
  created_at: string;
  assigned_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface CalibrationExample {
  example_id: string;
  project_id: string;
  metric: string;
  features_snapshot_id: string;
  verified_actual_id: string;
  target_value: string;
  unit: string;
  variance_reason: VarianceReason;
}

export interface CalibrationExampleView {
  example: CalibrationExample;
  approved: boolean;
  approval_task_id: string;
  task_status: string;
  task_updated_at: string;
  policy_version_id: string;
  policy_content_hash: string;
  created_at: string;
  approved_by: string | null;
  assigned_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
}

export interface ActualsContext {
  project_id: string;
  project_state: ApprovalState;
  policy_version_id: string;
  policy_content_hash: string;
  record_roles: ActorRole[];
  actual_review_role: ActorRole;
  variance_classifier_roles: ActorRole[];
  variance_review_role: ActorRole;
  calibration_approval_role: ActorRole;
  metric_definitions: ActualMetricDefinition[];
  required_metric_keys: string[];
  selected_metric: string;
  project_outcome_evidence_ids: string[];
  records: ActualReviewView[];
  evidence_candidates: ActualEvidenceCandidate[];
  candidates_truncated: boolean;
  variances: VarianceView[];
  calibration_examples: CalibrationExampleView[];
  next_cursor: string | null;
}

export interface ActualDecisionResult {
  record: ActualRecordView;
  approval_id: string;
  decision: "APPROVED" | "REJECTED";
}

export interface ActualComparisonResult {
  forecast: ForecastFact;
  actual: ActualFact;
  variance: VarianceRecord;
  variance_record_id: string;
  calibration_example: CalibrationExample | null;
  calibration_approved: boolean;
}

export interface VarianceDecisionResult {
  variance: VarianceView;
  approval_id: string;
  decision: "APPROVED" | "REJECTED";
  calibration_example: CalibrationExample | null;
}

export interface CalibrationDecisionResult {
  example: CalibrationExampleView;
  approval_id: string;
  decision: "APPROVED" | "REJECTED";
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

export interface ManualEvidenceDocument {
  document_id: string;
  document_revision_id: string;
  title: string;
  revision_label: string;
  original_filename: string;
  original_object_hash: string;
}

export interface ManualEvidenceContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  policy_version_id: string;
  review_role: ActorRole;
  allowed_project_states: ApprovalState[];
  documents: ManualEvidenceDocument[];
}

export interface ManualEvidenceReview {
  source_observation: EvidenceObservation;
  source_observation_hash: string;
  submission_reason: string;
  task_id: string;
  task_status: string;
  task_updated_at: string;
  task_created_by: string;
  policy_version_id: string;
  document_set_revision_id: string;
  review_role: ActorRole;
  decision_allowed: boolean;
  decision_blockers: string[];
  verified_observation_id: string | null;
}

export interface ManualEvidenceDecisionResult {
  review: ManualEvidenceReview;
  approval_id: string;
  decision: ApprovalDecision;
  verified_observation: EvidenceObservation | null;
}

export interface ReconciliationCandidate {
  observation: EvidenceObservation;
  adapter_qualification_id: string | null;
  adapter_status: string | null;
  adapter_valid_until: string | null;
  independence_domain: string | null;
  eligible: boolean;
  blockers: string[];
}

export interface ReconciliationContext {
  project_id: string;
  document_set_revision_id: string;
  reconciliation_version_id: string;
  available_field_names: string[];
  field_names_truncated: boolean;
  selected_field_name: string | null;
  candidates: ReconciliationCandidate[];
  candidates_truncated: boolean;
}

export interface ReconciliationOutcome {
  agreed_value: unknown | null;
  verified_observation_id: string | null;
  conflict: EvidenceConflict | null;
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

export type IntakeSeverity = "INFO" | "WARNING" | "BLOCKER";

export interface IntakeFinding {
  code: string;
  severity: IntakeSeverity;
  archive_path: string;
  message: string;
  details: Record<string, unknown>;
}

export interface IntakeSheetInspection {
  name: string;
  state: string;
  max_row: number;
  max_column: number;
  hidden_row_count: number;
  hidden_column_count: number;
  formula_cell_count: number;
  formula_without_cached_value_count: number;
  formula_error_cell_count: number;
  non_formula_error_cell_count: number;
}

export interface IntakeFileInspection {
  entry_id: string;
  archive_path: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
  nested_archive: boolean;
  corrupt: boolean;
  protected: boolean;
  unsupported: boolean;
  page_count: number | null;
  embedded_file_count: number;
  external_hyperlink_count: number;
  external_dependency_count: number;
  sheets: IntakeSheetInspection[];
  findings: IntakeFinding[];
}

export interface IntakeManifest {
  root_filename: string;
  root_sha256: string;
  entries: IntakeFileInspection[];
  findings: IntakeFinding[];
  all_files_processed: boolean;
}

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
  manifest: IntakeManifest | null;
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

export interface ExpertReworkIssue {
  kind: "BOQ_PRICE_ROW" | "RELEASE_FINDING";
  reference_id: string;
  code: string;
  comment: string;
}

export interface ExpertReworkResult {
  rework_request_id: string;
  project: ProjectView;
  snapshot_id: string;
  requested_state: ApprovalState;
  target_stage: ApprovalState;
  gate_hash: string;
  issues: ExpertReworkIssue[];
}

export interface AutomationReworkIssueReference {
  kind: string;
  reference_id: string;
  code: string;
}

export type AutomationReworkStatus =
  "PENDING_DISPATCH" | "STAGE_COMMAND_QUEUED" | "BLOCKED";

export interface AutomationReworkStatusItem {
  rework_request_id: string;
  project_id: string;
  snapshot_id: string;
  target_stage: ApprovalState;
  requested_by: string;
  requested_at: string;
  status: AutomationReworkStatus;
  dispatch_id: string | null;
  dispatch_hash: string | null;
  command_topic: string | null;
  command_delivery_status: string | null;
  integrity_error_code: string | null;
  issue_references: AutomationReworkIssueReference[];
}

export interface AutomationReworkStatusPage {
  project_id: string;
  items: AutomationReworkStatusItem[];
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

export type CostCategory =
  | "LABOUR"
  | "PLANT"
  | "MATERIAL"
  | "SUBCONTRACT"
  | "LOGISTICS"
  | "MOBILISATION"
  | "CONTRACT_FINANCE"
  | "RISK"
  | "OVERHEAD"
  | "PROFIT"
  | "TAX";

export type CostBasisKind =
  | "MARKET"
  | "NORMATIVE"
  | "APPROVED_ASSUMPTION"
  | "RISK_MODEL"
  | "DERIVED_MODEL";

export interface BoqCostComponent {
  semantic_key: string;
  category: CostCategory;
  basis_kind: CostBasisKind;
  sign: -1 | 1;
  factor_ids: string[];
}

export interface BoqLine {
  line_id: string;
  line_key: string;
  wbs_node_id: string;
  work_code: string;
  description: string;
  unit: string;
  status: string;
  critical_quantity: boolean;
  cost_components: BoqCostComponent[];
  supersedes_line_id: string | null;
  is_current: boolean;
  updated_at: string;
}

export interface BoqEvidenceCandidate {
  observation: EvidenceObservation;
  work_code: string;
  unit: string;
  description?: string | null;
}

export interface BoqAuthoringContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  evidence_field_name: string;
  evidence_candidates: BoqEvidenceCandidate[];
  candidates_truncated: boolean;
}

export interface BoqSpreadsheetCandidate {
  source_observation: EvidenceObservation;
  source_observation_hash: string;
  source_item_id: string;
  source_position_id: string;
  description: string;
  specification: string | null;
  source_reference: string | null;
  unit: string;
  quantity: string;
  worksheet_name: string;
  row_number: number;
  proposal_observation_id: string | null;
  proposal_task_status: string | null;
  verified_observation_id: string | null;
  proposal_allowed: boolean;
  proposal_blockers: string[];
  quantity_proposal_observation_id: string | null;
  quantity_proposal_task_status: string | null;
  verified_quantity_observation_id: string | null;
  quantity_proposal_allowed: boolean;
  quantity_proposal_blockers: string[];
}

export interface InitialQuantityEvidenceCandidate {
  observation: EvidenceObservation;
  observation_hash: string;
  source_item_id: string;
  value: string;
  unit: string;
}

export interface InitialQuantityContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  line: BoqLine;
  source_item_id: string | null;
  quantity_policy_version_id: string | null;
  evidence_candidates: InitialQuantityEvidenceCandidate[];
  current_quantity_id: string | null;
  current_quantity_status: string | null;
  recording_allowed: boolean;
  recording_blockers: string[];
}

export interface BoqSpreadsheetCandidateContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  candidates: BoqSpreadsheetCandidate[];
  candidates_truncated: boolean;
}

export interface BoqLineReview {
  line: BoqLine;
  created_by: string;
  document_set_revision_id: string;
  evidence_observations: EvidenceObservation[];
  verification_allowed: boolean;
  verification_blockers: string[];
}

export interface ScopeFinding {
  finding_id: string;
  rule_id: string;
  wbs_node_id: string;
  required_work_code: string;
  severity: string;
  reason: string;
  supporting_entity_ids: string[];
  resolved: boolean;
  resolved_by: string | null;
  resolution_reason: string | null;
}

export interface ScopeRun {
  evaluation: {
    rule_pack_version_id: string;
    evaluated_work_codes: string[];
    findings: ScopeFinding[];
  } | null;
  validation_findings: ValidationFinding[];
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

export type PriceSourceType =
  | "FGIS_CS"
  | "WON_TENDER"
  | "MARKETPLACE"
  | "SUPPLIER_WEBSITE"
  | "SUPPLIER_QUOTE"
  | "OTHER_OFFICIAL";

export interface PriceSourceReference {
  source_type: PriceSourceType;
  display_name: string;
  source_item_name: string;
  source_record_id: string;
  source_uri: string | null;
}

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
  source_reference: PriceSourceReference;
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

export interface FgisCsStoredExchange {
  request_uri: string;
  response_object_hash: string;
  response_object_key: string;
  response_size_bytes: number;
}

export interface FgisCsAcquisition {
  acquisition_id: string;
  project_id: string;
  item_id: string;
  boq_item_name: string;
  boq_unit: string;
  nomenclature_match_id: string;
  canonical_item_id: string;
  document_set_revision_id: string;
  policy_version_id: string;
  adapter_qualification_id: string;
  status: "UNVERIFIED";
  basis_current: boolean;
  ready_for_pricing: false;
  pricing_blockers: string[];
  artifact_object_hash: string;
  artifact: {
    schema_version: string;
    request_context_hash: string;
    request: {
      subject_name: string;
      price_zone_name: string | null;
      period_name: string;
      resource_code: string;
    };
    result: {
      schema_version: string;
      subject: { id: number; name: string };
      price_zone: { id: number; name: string };
      period: { id: number; name: string };
      requested_resource_code: string;
      price: null | {
        source_record_id: string;
        resource_code: string;
        source_item_name: string;
        unit: string;
        aggregated_price: string;
        estimated_price: string;
        distance_price: string;
        procure_storage_cost_percent: string;
        source_amount_literals: Record<string, string>;
        ksr_type: number;
      };
      public_page_uri: string;
      api_request_uri: string;
      response_sha256: string;
      retrieved_at: string;
      ready_for_pricing: false;
      pricing_blockers: string[];
    };
    exchanges: FgisCsStoredExchange[];
  };
}

export interface FgisCsAcquisitionList {
  project_id: string;
  item_id: string;
  boq_item_name: string;
  boq_unit: string;
  acquisitions: FgisCsAcquisition[];
  release_warning: string;
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
    missing_source_groups: string[];
    reason: string;
  };
  relative_spread: string | null;
  approval_task_ids: string[];
  rfq_request_id: string | null;
  project_state: string;
}

export interface BoqPriceNameMatch {
  match_id: string;
  status: string;
  match_class: NomenclatureMatchClass;
  boq_item_name: string;
  source_item_id: string;
  canonical_item_id: string | null;
  source_attributes: Record<string, string>;
  canonical_attributes: Record<string, string>;
  mismatched_attributes: string[];
  missing_attributes: string[];
  catalog_version_id: string;
  assessment_method: string | null;
}

export interface BoqSourcePrice {
  quote_id: string;
  evidence_class: PriceEvidenceClass;
  source_reference: PriceSourceReference;
  source_observation_id: string;
  source_origin_id: string;
  source_locator: string;
  source_document_revision_id: string;
  observed_at: string;
  quote_date: string;
  valid_until: string | null;
  available: boolean | null;
  lead_time_days: number | null;
  raw_amount: string;
  raw_currency: string;
  raw_unit: string;
  normalized_prices: NormalizedPrice[];
  technical_attributes: Record<string, string>;
}

export interface BoqProposedPrice {
  status: "VERIFIED" | "BLOCKED";
  workflow_status: string;
  amount_per_unit: string | null;
  currency: string | null;
  unit: string | null;
  decision_id: string | null;
  as_of: string | null;
  selection_method: string | null;
  normalized_price_ids: string[];
  rationale: string[];
}

export interface BoqPriceMatrixRow {
  row_id: string;
  boq_line_id: string;
  line_key: string;
  wbs_node_id: string;
  work_code: string;
  boq_item_name: string;
  boq_unit: string;
  quantity: string | null;
  quantity_status: string;
  item_id: string;
  cost_category: string | null;
  basis_kind: string | null;
  row_status: "VERIFIED" | "BLOCKED";
  blockers: string[];
  name_match: BoqPriceNameMatch | null;
  won_tender_prices: BoqSourcePrice[];
  fgis_cs_prices: BoqSourcePrice[];
  market_prices: BoqSourcePrice[];
  other_prices: BoqSourcePrice[];
  proposed_price: BoqProposedPrice;
}

export interface BoqPriceMatrix {
  project_id: string;
  generated_at: string;
  rows: BoqPriceMatrixRow[];
  blocked_row_count: number;
  release_warning: string;
}

export type NomenclatureMatchClass =
  | "EXACT"
  | "FUNCTIONAL_ANALOGUE"
  | "CONDITIONALLY_ACCEPTABLE_ANALOGUE"
  | "TECHNICALLY_UNACCEPTABLE"
  | "INSUFFICIENT_DATA";

export interface NomenclatureMatch {
  match_id: string;
  source_item_id: string;
  canonical_item_id: string | null;
  match_class: NomenclatureMatchClass;
  required_critical_attributes: string[];
  source_attributes: Record<string, string>;
  canonical_attributes: Record<string, string>;
  mismatched_attributes: string[];
  missing_attributes: string[];
  verified_by: string | null;
  verified_at: string | null;
}

export interface NomenclatureMatchView {
  match: NomenclatureMatch;
  status: string;
  catalog_version_id: string;
  supersedes_match_id: string | null;
  approval_task_ids: string[];
}

export interface CatalogItem {
  canonical_item_id: string;
  attributes: Record<string, string>;
  critical_attributes: string[];
  critical_price: boolean;
  retrieval_exact_identifier: boolean;
  retrieval_matched_terms: string[];
  retrieval_matched_critical_attributes: string[];
}

export interface NomenclatureSourceItem {
  source_item_id: string;
  boq_line_id: string;
  line_key: string;
  wbs_node_id: string;
  work_code: string;
  description: string;
  unit: string;
}

export interface NomenclatureEvidenceCandidate {
  observation: EvidenceObservation;
  attributes: Record<string, string>;
}

export interface NomenclatureContext {
  project_id: string;
  project_state: ApprovalState;
  document_set_revision_id: string;
  catalog_version_id: string;
  source_items: NomenclatureSourceItem[];
  selected_source_item_id: string | null;
  selected_source_description: string | null;
  catalog_items: CatalogItem[];
  catalog_items_truncated: boolean;
  evidence_field_name: string;
  evidence_candidates: NomenclatureEvidenceCandidate[];
  evidence_candidates_truncated: boolean;
  retrieval_notice: string;
}

export interface NomenclatureReviewContext {
  match: NomenclatureMatchView;
  source_attributes_observation_id: string;
  source_observation: EvidenceObservation;
  proposal_reason: string | null;
  equivalence_rule_version_id: string | null;
  approval_task_statuses: Record<string, string>;
  finalization_allowed: boolean;
  finalization_blockers: string[];
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

export interface ScenarioOverride {
  cost_input_id: string;
  quantity: string | null;
  unit_rate: string | null;
  factor_values: Record<string, string>;
  evidence_or_assumption_id: string;
  reason: string;
}

export interface ScenarioDefinition {
  scenario_id: string;
  scenario_version: string;
  name: string;
  overrides: ScenarioOverride[];
}

export interface ScenarioSnapshot {
  snapshot_id: string;
  calculation_run_id: string;
  document_set_revision_id: string;
  snapshot_hash: string;
  currency: string | null;
  grand_total: string | null;
  independent_validation_passed: boolean | null;
  created_by: string;
  created_at: string;
  integrity_valid: boolean;
  integrity_error: string | null;
}

export interface ScenarioComparison {
  scenario_run_id: string;
  scenario_key: string;
  scenario_name: string;
  base_snapshot_id: string;
  scenario_policy_version_id: string;
  status: string;
  currency: string | null;
  base_grand_total: string | null;
  scenario_grand_total: string | null;
  absolute_delta: string | null;
  relative_delta_percent: string | null;
  independent_validation_passed: boolean | null;
  executed_by: string | null;
  created_at: string;
  integrity_valid: boolean;
  integrity_error: string | null;
}

export interface ScenarioContext {
  project_id: string;
  project_state: ApprovalState;
  current_document_set_revision_id: string | null;
  scenario_policy_version_id: string | null;
  selected_snapshot_id: string | null;
  snapshots: ScenarioSnapshot[];
  snapshots_truncated: boolean;
  definitions: ScenarioDefinition[];
  comparisons: ScenarioComparison[];
  comparisons_truncated: boolean;
  blockers: string[];
}

export interface ScenarioExecution {
  scenario_run_id: string;
  base_snapshot_id: string;
  scenario_policy_version_id: string;
  definition: ScenarioDefinition;
  result: {
    scenario_id: string;
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
  };
}

export interface DevelopmentIdentity {
  actorId: string;
  organizationId: string;
  roles: ActorRole[];
}
