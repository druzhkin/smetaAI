export type AuthenticationMode = "OIDC" | "DEVELOPMENT" | "UNAVAILABLE";

export interface RuntimeConfig {
  environment: string;
  authentication_mode: AuthenticationMode;
  oidc_authority: string | null;
  oidc_client_id: string | null;
  oidc_scope: string;
  api_base_path: string;
  application_version: string;
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
