from __future__ import annotations

from typing import Any

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    NormativeCalculationRow,
    ObservationRow,
    RiskCalculationRow,
)


class ObservationLineageNode(DomainModel):
    observation_id: str
    status: str
    payload: dict[str, Any]
    document: dict[str, Any]
    source_observations: tuple[ObservationLineageNode, ...] = ()


class EvidenceLineage(DomainModel):
    basis_id: str
    basis_type: str
    status: str
    payload: dict[str, Any]
    document: dict[str, Any] | None = None
    source_observations: tuple[ObservationLineageNode, ...] = ()


class CostInputLineage(DomainModel):
    cost_input_id: str
    semantic_key: str
    category: str
    payload: dict[str, Any]
    evidence: EvidenceLineage


class SnapshotLineage(DomainModel):
    snapshot_id: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_set_revision_id: str
    calculation_run_id: str
    calculation: dict[str, Any]
    immutable_snapshot: dict[str, Any]
    cost_inputs: tuple[CostInputLineage, ...]


class LineageService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store

    def snapshot_lineage(
        self,
        *,
        actor: Actor,
        project_id: str,
        snapshot_id: str,
    ) -> SnapshotLineage:
        required_roles = (
            ActorRole.ESTIMATOR,
            ActorRole.PROCUREMENT,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.REVIEWER,
            ActorRole.APPROVER,
            ActorRole.METHODOLOGY_OWNER,
            ActorRole.AUDITOR,
        )
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            required_roles=required_roles,
        )
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == snapshot_id,
                CalculationSnapshotRow.project_id == project_id,
            )
        )
        if snapshot is None:
            raise LookupError(snapshot_id)
        run = self.session.scalar(
            select(CalculationRunRow).where(
                CalculationRunRow.id == snapshot.calculation_run_id,
                CalculationRunRow.project_id == project_id,
            )
        )
        if run is None:
            raise RuntimeError("Snapshot calculation run is missing")
        immutable_snapshot = read_verified_snapshot(
            object_store=self.object_store,
            snapshot=snapshot,
        )
        cost_rows = list(
            self.session.scalars(
                select(CostInputRow)
                .where(
                    CostInputRow.project_id == project_id,
                    CostInputRow.calculation_run_id == run.id,
                )
                .order_by(CostInputRow.id)
            )
        )
        lineages = tuple(self._cost_input_lineage(project_id, row) for row in cost_rows)
        return SnapshotLineage(
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            document_set_revision_id=snapshot.document_set_revision_id,
            calculation_run_id=run.id,
            calculation={
                "engine_version": run.engine_version,
                "status": run.status,
                "currency": run.currency,
                "grand_total": str(run.grand_total),
                **run.payload,
            },
            immutable_snapshot=immutable_snapshot,
            cost_inputs=lineages,
        )

    def _cost_input_lineage(
        self,
        project_id: str,
        row: CostInputRow,
    ) -> CostInputLineage:
        basis_id = row.amount_basis_id
        if not basis_id:
            raise RuntimeError(f"Cost input {row.id} has no evidence basis")
        evidence = self._resolve_evidence(project_id, basis_id)
        return CostInputLineage(
            cost_input_id=row.id,
            semantic_key=row.semantic_key,
            category=row.category,
            payload=row.payload,
            evidence=evidence,
        )

    def _resolve_evidence(self, project_id: str, basis_id: str) -> EvidenceLineage:
        observation = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == basis_id,
                ObservationRow.project_id == project_id,
            )
        )
        if observation is not None:
            document = self._document_lineage(project_id, observation.document_revision_id)
            sources = self._source_observation_lineage(
                project_id,
                observation,
                visited={observation.id},
            )
            return EvidenceLineage(
                basis_id=basis_id,
                basis_type="OBSERVATION",
                status=observation.status,
                payload=observation.payload,
                document=document,
                source_observations=sources,
            )
        approval = self.session.scalar(
            select(ApprovalRecordRow)
            .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
            .where(
                ApprovalRecordRow.id == basis_id,
                ApprovalTaskRow.project_id == project_id,
            )
        )
        if approval is not None:
            return EvidenceLineage(
                basis_id=basis_id,
                basis_type="APPROVED_ASSUMPTION",
                status=approval.decision,
                payload={
                    **approval.payload,
                    "decided_by": approval.decided_by,
                    "decided_at": approval.decided_at.isoformat(),
                    "reason": approval.reason,
                },
            )
        normative = self.session.scalar(
            select(NormativeCalculationRow).where(
                NormativeCalculationRow.id == basis_id,
                NormativeCalculationRow.project_id == project_id,
            )
        )
        if normative is not None:
            return EvidenceLineage(
                basis_id=basis_id,
                basis_type="NORMATIVE_RATE",
                status=normative.status,
                payload={
                    **normative.payload,
                    "adapter_qualification_id": normative.adapter_qualification_id,
                    "normative_basis_version": normative.normative_basis_version,
                    "artifact_hash": normative.artifact_hash,
                },
            )
        risk = self.session.scalar(
            select(RiskCalculationRow).where(
                RiskCalculationRow.id == basis_id,
                RiskCalculationRow.project_id == project_id,
            )
        )
        if risk is not None:
            return EvidenceLineage(
                basis_id=basis_id,
                basis_type="RISK_RESERVE",
                status=risk.status,
                payload={
                    **risk.payload,
                    "policy_version_id": risk.policy_version_id,
                    "expected_reserve": str(risk.expected_reserve),
                    "currency": risk.currency,
                    "unit": risk.unit,
                },
            )
        raise RuntimeError(f"Evidence basis does not resolve: {basis_id}")

    def _source_observation_lineage(
        self,
        project_id: str,
        observation: ObservationRow,
        *,
        visited: set[str],
    ) -> tuple[ObservationLineageNode, ...]:
        raw_ids = observation.payload.get("source_observation_ids", [])
        if not isinstance(raw_ids, list):
            raise RuntimeError("Observation source_observation_ids is not a list")
        source_ids = {str(item) for item in raw_ids}
        if not source_ids:
            return ()
        if source_ids & visited:
            raise RuntimeError("Evidence observation lineage contains a cycle")
        source_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(source_ids),
                )
                .order_by(ObservationRow.id)
            )
        )
        if len(source_rows) != len(source_ids):
            raise RuntimeError("Evidence observation lineage has missing source nodes")
        nodes: list[ObservationLineageNode] = []
        for row in source_rows:
            nodes.append(
                ObservationLineageNode(
                    observation_id=row.id,
                    status=row.status,
                    payload=row.payload,
                    document=self._document_lineage(
                        project_id,
                        row.document_revision_id,
                    ),
                    source_observations=self._source_observation_lineage(
                        project_id,
                        row,
                        visited={*visited, row.id},
                    ),
                )
            )
        return tuple(nodes)

    def _document_lineage(
        self,
        project_id: str,
        document_revision_id: str,
    ) -> dict[str, Any]:
        result = self.session.execute(
            select(DocumentRevisionRow, DocumentRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRevisionRow.id == document_revision_id,
                DocumentRow.project_id == project_id,
            )
        ).one_or_none()
        if result is None:
            raise RuntimeError("Evidence document revision is missing")
        revision, document = result
        return {
            "document_id": document.id,
            "logical_key": document.logical_key,
            "title": document.title,
            "document_type": document.document_type,
            "revision_id": revision.id,
            "revision_label": revision.revision_label,
            "object_hash": revision.object_hash,
            "original_filename": revision.original_filename,
            "inspection": revision.inspection_payload,
        }
