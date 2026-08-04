from __future__ import annotations

# ruff: noqa: RUF001 -- Russian operator-facing text is intentional.
import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, status
from pydantic import Field, model_validator

from tenderguard.application.diagnostic_research import (
    DiagnosticResearchBundle,
    DiagnosticResearchLoadError,
    DiagnosticResearchReferences,
    DiagnosticRowResearch,
    build_diagnostic_row_research,
    load_diagnostic_research,
)
from tenderguard.application.pricing import (
    BoqPriceMatrixRowView,
    BoqPriceMatrixView,
    BoqProposedPriceView,
)
from tenderguard.application.projects import ProjectView
from tenderguard.application.workbench import (
    FINANCIAL_READ_ROLES,
    ProjectAccessView,
    ProjectPortfolioItem,
    ProjectRecord,
    ProjectRecordSection,
    ProjectWorkbench,
    WorkbenchMetric,
)
from tenderguard.domain.boq_spreadsheet import BoqXlsxExtractionResult
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    FindingCode,
    ProjectAccessLevel,
    Severity,
)
from tenderguard.domain.models import DomainModel, GateDecision, ValidationFinding
from tenderguard.infrastructure.auth import Actor

DIAGNOSTIC_PROJECT_CURSOR = "diagnostic-project:v1:database-first-page"
MAX_DIAGNOSTIC_MANIFEST_BYTES = 64 * 1024


class DiagnosticProjectManifest(DomainModel):
    schema_version: Literal[
        "tenderguard.diagnostic-project/v1",
        "tenderguard.diagnostic-project/v2",
    ] = (
        "tenderguard.diagnostic-project/v1"
    )
    project_id: str = Field(pattern=r"^diagnostic-[a-z0-9][a-z0-9-]{0,52}$")
    organization_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    extraction_path: str = Field(min_length=1, max_length=4000)
    extraction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research: DiagnosticResearchReferences | None = None

    @model_validator(mode="after")
    def metadata_is_normalized(self) -> DiagnosticProjectManifest:
        for value in (
            self.project_id,
            self.organization_id,
            self.code,
            self.name,
            self.extraction_path,
        ):
            if value != value.strip():
                raise ValueError("Diagnostic project metadata must be normalized")
        if self.schema_version == "tenderguard.diagnostic-project/v1":
            if self.research is not None:
                raise ValueError("Diagnostic project v1 cannot contain research packages")
        elif self.research is None:
            raise ValueError("Diagnostic project v2 requires hash-pinned research packages")
        return self


class DiagnosticProjectLoadError(RuntimeError):
    pass


class DiagnosticProject:
    """Read-only development view over a hash-pinned XLSX extraction.

    This adapter deliberately does not persist BoQ lines, price evidence, decisions,
    or calculation results. It exists only to make a real diagnostic extraction
    inspectable through the operator UI while every financial output stays blocked.
    """

    def __init__(
        self,
        *,
        manifest: DiagnosticProjectManifest,
        extraction: BoqXlsxExtractionResult,
        research: DiagnosticResearchBundle | None = None,
    ) -> None:
        self.manifest = manifest
        self.extraction = extraction
        self.research = research
        self.row_research: dict[str, DiagnosticRowResearch] = (
            build_diagnostic_row_research(research) if research is not None else {}
        )

    @classmethod
    def load(
        cls,
        manifest_path: Path,
        *,
        max_extraction_bytes: int,
    ) -> DiagnosticProject:
        try:
            resolved_manifest = manifest_path.resolve(strict=True)
        except OSError as error:
            raise DiagnosticProjectLoadError(
                "Diagnostic project manifest does not exist"
            ) from error
        manifest_payload = _read_bounded(
            resolved_manifest,
            maximum_bytes=MAX_DIAGNOSTIC_MANIFEST_BYTES,
            label="diagnostic project manifest",
        )
        try:
            manifest = DiagnosticProjectManifest.model_validate_json(manifest_payload)
        except ValueError as error:
            raise DiagnosticProjectLoadError("Diagnostic project manifest is invalid") from error

        raw_extraction_path = Path(manifest.extraction_path)
        try:
            extraction_path = (
                raw_extraction_path
                if raw_extraction_path.is_absolute()
                else resolved_manifest.parent / raw_extraction_path
            ).resolve(strict=True)
        except OSError as error:
            raise DiagnosticProjectLoadError(
                "Diagnostic BoQ extraction does not exist"
            ) from error
        extraction_payload = _read_bounded(
            extraction_path,
            maximum_bytes=max_extraction_bytes,
            label="diagnostic BoQ extraction",
        )
        actual_hash = hashlib.sha256(extraction_payload).hexdigest()
        if not hmac.compare_digest(actual_hash, manifest.extraction_sha256):
            raise DiagnosticProjectLoadError(
                "Diagnostic BoQ extraction differs from the hash-pinned manifest"
            )
        try:
            extraction = BoqXlsxExtractionResult.model_validate_json(extraction_payload)
        except ValueError as error:
            raise DiagnosticProjectLoadError("Diagnostic BoQ extraction is invalid") from error
        if not extraction.candidates:
            raise DiagnosticProjectLoadError(
                "Diagnostic BoQ extraction contains no candidate rows"
            )
        research = None
        if manifest.research is not None:
            try:
                research = load_diagnostic_research(
                    references=manifest.research,
                    base_directory=resolved_manifest.parent,
                    extraction=extraction,
                    maximum_file_bytes=max_extraction_bytes,
                )
            except DiagnosticResearchLoadError as error:
                raise DiagnosticProjectLoadError(str(error)) from error
        return cls(manifest=manifest, extraction=extraction, research=research)

    @property
    def project_id(self) -> str:
        return self.manifest.project_id

    @property
    def generated_at(self) -> datetime:
        return (
            self.research.generated_at
            if self.research is not None
            else self.extraction.extracted_at
        )

    def is_project(self, project_id: str) -> bool:
        return hmac.compare_digest(project_id, self.manifest.project_id)

    def is_visible_in_portfolio(
        self,
        *,
        actor: Actor,
        query: str | None,
        states: frozenset[ApprovalState],
    ) -> bool:
        if not self._actor_can_read(actor):
            return False
        if states and ApprovalState.BLOCKED not in states:
            return False
        normalized_query = query.strip().casefold() if query else ""
        if not normalized_query:
            return True
        return normalized_query in self.manifest.code.casefold() or normalized_query in (
            self.manifest.name.casefold()
        )

    def project_view(self, *, actor: Actor) -> ProjectView:
        self._authorize(actor)
        return ProjectView(
            id=self.manifest.project_id,
            organization_id=self.manifest.organization_id,
            code=self.manifest.code,
            name=self.manifest.name,
            state=ApprovalState.BLOCKED,
            row_version=0,
            current_document_set_revision_id=None,
        )

    def portfolio_item(self, *, actor: Actor) -> ProjectPortfolioItem:
        return ProjectPortfolioItem(
            project=self.project_view(actor=actor),
            access=self._access(actor),
            open_approval_count=0,
            unresolved_blocker_count=len(self._release_findings()),
            latest_total=None,
            latest_currency=None,
            updated_at=self.generated_at,
        )

    def workbench(self, *, actor: Actor) -> ProjectWorkbench:
        project = self.project_view(actor=actor)
        findings = self._release_findings()
        candidate_ids = tuple(
            candidate.provisional_candidate_id for candidate in self.extraction.candidates
        )
        source_record = ProjectRecord(
            id=f"{self.project_id}:xlsx-extraction",
            section=ProjectRecordSection.BOQ_SCOPE,
            kind="DIAGNOSTIC_XLSX_EXTRACTION",
            title=f"Извлечено строк из XLSX: {len(candidate_ids)}",
            subtitle=(
                "Строки доступны для анализа, но не являются подтверждённой ВОР "
                "и не допускают выпуск цены."
            ),
            status="BLOCKED",
            severity=Severity.BLOCKER.value,
            current=False,
            occurred_at=self.generated_at,
            attributes={
                "archive_path": self.extraction.archive_path,
                "workbook_object_sha256": self.extraction.workbook_object_sha256,
                "profile_version_id": self.extraction.profile_version_id,
                "profile_content_hash": self.extraction.profile_content_hash,
                "extraction_sha256": self.manifest.extraction_sha256,
                "candidate_count": len(candidate_ids),
                "global_blockers": list(self.extraction.global_blockers),
                "workflow_blockers": list(self.extraction.workflow_blockers),
                "research_package_hashes": (
                    self.research.package_hashes if self.research is not None else {}
                ),
                "research_route_count": len(self.row_research),
                "research_source_candidate_count": sum(
                    len(item.fgis_candidates) + len(item.market_candidates)
                    for item in self.row_research.values()
                ),
            },
        )
        research_candidate_count = sum(
            len(item.fgis_candidates) + len(item.market_candidates)
            for item in self.row_research.values()
        )
        metrics = (
            WorkbenchMetric(
                code="DOCUMENTS",
                label="Управляемые документы",
                value=0,
                blocking=1,
            ),
            WorkbenchMetric(
                code="EXTRACTED_ROWS",
                label="Извлечённые строки",
                value=len(candidate_ids),
                blocking=len(candidate_ids),
            ),
            WorkbenchMetric(
                code="RESEARCH_ROUTES",
                label="Строки с маршрутом исследования",
                value=len(self.row_research),
                blocking=len(candidate_ids),
            ),
            WorkbenchMetric(
                code="RESEARCH_CANDIDATES",
                label="Сырые кандидаты источников",
                value=research_candidate_count,
                blocking=research_candidate_count,
            ),
            WorkbenchMetric(
                code="MATCHED_ROWS",
                label="Подтверждённые сопоставления",
                value=0,
                blocking=len(candidate_ids),
            ),
            WorkbenchMetric(
                code="PRICED_ROWS",
                label="Строки с проверенной ценой",
                value=0,
                blocking=len(candidate_ids),
            ),
            WorkbenchMetric(
                code="CALCULATIONS",
                label="Зафиксированные расчёты",
                value=0,
                blocking=1,
            ),
        )
        return ProjectWorkbench(
            project=project,
            access=self._access(actor),
            release_decision=GateDecision(
                requested_state=ApprovalState.APPROVED_FOR_BID,
                allowed=False,
                resulting_state=ApprovalState.BLOCKED,
                findings=findings,
            ),
            metrics=metrics,
            attention=(source_record,),
            recent_activity=(source_record,),
            latest_total=None,
            latest_currency=None,
            generated_at=self.generated_at,
        )

    def price_matrix(self, *, actor: Actor) -> BoqPriceMatrixView:
        self._authorize(actor)
        rows: list[BoqPriceMatrixRowView] = []
        common_blockers = (
            *self.extraction.global_blockers,
            *self.extraction.workflow_blockers,
            "BOQ_LINE_NOT_VERIFIED",
            "NOMENCLATURE_MATCH_MISSING",
            "WON_TENDER_PRICE_MISSING",
            "FGIS_CS_PRICE_MISSING",
            "MARKET_PRICE_MISSING",
            "PRICE_POLICY_INTEGRITY_FAILED",
            "PRICE_DECISION_MISSING",
            "CALCULATION_SNAPSHOT_MISSING",
            "BID_RELEASE_NOT_APPROVED",
        )
        for candidate in self.extraction.candidates:
            research = self.row_research.get(candidate.provisional_candidate_id)
            quantity_status = "UNVERIFIED" if candidate.quantity is not None else "MISSING"
            quantity_blocker = (
                "QUANTITY_NOT_VERIFIED" if candidate.quantity is not None else "QUANTITY_MISSING"
            )
            blockers = tuple(
                dict.fromkeys(
                    (
                        *common_blockers,
                        quantity_blocker,
                        *candidate.blockers,
                        *(research.blockers if research is not None else ()),
                    )
                )
            )
            research_rationale = (
                research.route.rationale
                if research is not None
                else (
                    "Для строки ещё не сформирован зафиксированный маршрут исследования.",
                )
            )
            rows.append(
                BoqPriceMatrixRowView(
                    row_id=candidate.provisional_candidate_id,
                    boq_line_id=candidate.provisional_candidate_id,
                    line_key=(candidate.source_position_id or f"XLSX-ROW-{candidate.row_number}"),
                    wbs_node_id="UNMAPPED",
                    work_code="UNMAPPED",
                    boq_item_name=candidate.description or "NOT_EXTRACTED",
                    boq_unit=candidate.unit or "NOT_EXTRACTED",
                    quantity=candidate.quantity,
                    quantity_status=quantity_status,
                    item_id=candidate.provisional_candidate_id,
                    cost_category=None,
                    basis_kind=None,
                    row_status="BLOCKED",
                    blockers=blockers,
                    name_match=None,
                    won_tender_prices=(),
                    fgis_cs_prices=(),
                    market_prices=(),
                    other_prices=(),
                    research_route=(research.route if research is not None else None),
                    won_tender_research_candidates=(),
                    fgis_cs_research_candidates=(
                        research.fgis_candidates if research is not None else ()
                    ),
                    market_research_candidates=(
                        research.market_candidates if research is not None else ()
                    ),
                    proposed_price=BoqProposedPriceView(
                        status="BLOCKED",
                        workflow_status="DIAGNOSTIC_ONLY",
                        rationale=(
                            "Строка извлечена из XLSX как непроверенное свидетельство.",
                            *research_rationale,
                            "Цена скрыта до управляемого сопоставления номенклатуры, "
                            "сбора источников и воспроизводимого расчёта.",
                            *tuple(f"Blocker: {blocker}" for blocker in blockers),
                        ),
                    ),
                )
            )
        return BoqPriceMatrixView(
            project_id=self.project_id,
            generated_at=self.generated_at,
            rows=tuple(rows),
            blocked_row_count=len(rows),
            release_warning=(
                "Диагностический импорт: исходные суммы ФГИС ЦС и рынка показаны "
                "только как кандидаты, зафиксированные контрольными суммами. "
                "Они не нормализованы, "
                "не являются предлагаемой ценой; итоги и допуск к заявке отсутствуют."
            ),
        )

    def _release_findings(self) -> tuple[ValidationFinding, ...]:
        candidate_ids = tuple(
            candidate.provisional_candidate_id for candidate in self.extraction.candidates
        )
        provenance = {
            "mode": "DIAGNOSTIC_XLSX_EXTRACTION",
            "candidate_count": len(candidate_ids),
            "profile_version_id": self.extraction.profile_version_id,
            "workbook_object_sha256": self.extraction.workbook_object_sha256,
            "extraction_sha256": self.manifest.extraction_sha256,
            "research_package_hashes": (
                self.research.package_hashes if self.research is not None else {}
            ),
        }
        return (
            ValidationFinding(
                code=FindingCode.COST_WITHOUT_BASIS,
                severity=Severity.BLOCKER,
                message="Для извлечённых строк отсутствуют проверенные ценовые основания.",
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.TECHNICAL_ANALOGUE_UNVERIFIED,
                severity=Severity.BLOCKER,
                message="Номенклатура извлечённых строк не сопоставлена.",
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.KEY_QUANTITY_UNVERIFIED,
                severity=Severity.BLOCKER,
                message="Объёмы XLSX извлечены, но не подтверждены.",
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.CURRENT_DOCUMENT_SET_NOT_CONFIRMED,
                severity=Severity.BLOCKER,
                message="Диагностический файл не является подтверждённым комплектом документов.",
                entity_ids=(self.project_id,),
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.NORMATIVE_CALCULATION_MISSING,
                severity=Severity.BLOCKER,
                message="Проверенный нормативный расчёт отсутствует.",
                entity_ids=(self.project_id,),
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.CALCULATION_SNAPSHOT_MISSING,
                severity=Severity.BLOCKER,
                message="Зафиксированный воспроизводимый расчёт отсутствует.",
                entity_ids=(self.project_id,),
                details=provenance,
            ),
            ValidationFinding(
                code=FindingCode.INDEPENDENT_VALIDATION_MISSING,
                severity=Severity.BLOCKER,
                message="Независимый повторный пересчёт отсутствует.",
                entity_ids=(self.project_id,),
                details=provenance,
            ),
        )

    def _access(self, actor: Actor) -> ProjectAccessView:
        self._authorize(actor)
        roles = tuple(
            sorted(
                actor.roles.intersection(FINANCIAL_READ_ROLES),
                key=lambda role: role.value,
            )
        )
        return ProjectAccessView(
            access_level=(
                ProjectAccessLevel.OWNER
                if ActorRole.ESTIMATOR in roles
                else ProjectAccessLevel.MEMBER
            ),
            roles=roles,
        )

    def _actor_can_read(self, actor: Actor) -> bool:
        return (
            ActorRole.SYSTEM not in actor.roles
            and actor.organization_id == self.manifest.organization_id
            and bool(actor.roles.intersection(FINANCIAL_READ_ROLES))
        )

    def _authorize(self, actor: Actor) -> None:
        if actor.organization_id != self.manifest.organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if ActorRole.SYSTEM in actor.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SYSTEM identities cannot use diagnostic operator views",
            )
        actor.require_any(*FINANCIAL_READ_ROLES)


def _read_bounded(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DiagnosticProjectLoadError(f"Cannot inspect {label}") from error
    if size <= 0 or size > maximum_bytes:
        raise DiagnosticProjectLoadError(f"{label.capitalize()} size is invalid")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise DiagnosticProjectLoadError(f"Cannot read {label}") from error
    if len(payload) != size:
        raise DiagnosticProjectLoadError(f"{label.capitalize()} changed while loading")
    return payload
