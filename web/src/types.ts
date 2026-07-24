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
  allowed: boolean;
  resulting_state: ApprovalState;
  findings: ValidationFinding[];
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

export interface DevelopmentIdentity {
  actorId: string;
  organizationId: string;
  roles: ActorRole[];
}
