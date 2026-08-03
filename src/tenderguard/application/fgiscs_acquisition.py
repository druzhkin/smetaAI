from __future__ import annotations

import hashlib
from io import BytesIO

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
)
from tenderguard.application.document_set_integrity import (
    require_confirmed_document_set_integrity,
)
from tenderguard.application.pricing import PricingService
from tenderguard.application.projects import ProjectService, SystemProjectAccess
from tenderguard.config import Settings
from tenderguard.domain.common import canonical_json, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState, EvidenceMethod, VerificationStatus
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore, StoredObject
from tenderguard.infrastructure.orm import AdapterQualificationRow, FgisCsAcquisitionRow
from tenderguard.integrations.fgiscs_public import (
    FgisCsMaterialAcquisition,
    FgisCsMaterialLookupRequest,
    FgisCsMaterialLookupResult,
    FgisCsPublicApi,
    FgisCsRawHttpExchange,
    replay_fgiscs_material_acquisition,
)

FGIS_CS_ACQUISITION_POLICY_PURPOSE = "fgis_cs_acquisition_policy"
FGIS_CS_ACQUISITION_POLICY_KIND = "fgis_cs_acquisition_policy"
FGIS_CS_ACQUISITION_ARTIFACT_SCHEMA = "fgiscs-acquisition-artifact/v1"


class FgisCsAcquisitionPolicy(DomainModel):
    subject_name: str = Field(min_length=1, max_length=500)
    price_zone_name: str | None = Field(default=None, min_length=1, max_length=500)
    period_name: str = Field(min_length=1, max_length=200)
    allowed_project_states: tuple[ApprovalState, ...] = Field(min_length=1)
    required_adapter_name: str = Field(min_length=1, max_length=200)
    required_adapter_version: str = Field(min_length=1, max_length=200)
    source_origin_id: str = Field(min_length=1, max_length=500)

    @field_validator(
        "subject_name",
        "price_zone_name",
        "period_name",
        "required_adapter_name",
        "required_adapter_version",
        "source_origin_id",
    )
    @classmethod
    def literals_are_exact_single_lines(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("FGIS CS policy literals must be exact single-line values")
        return value

    @field_validator("allowed_project_states")
    @classmethod
    def states_are_unique(
        cls,
        values: tuple[ApprovalState, ...],
    ) -> tuple[ApprovalState, ...]:
        if len(values) != len(set(values)):
            raise ValueError("FGIS CS acquisition project states must be unique")
        return values

    @model_validator(mode="after")
    def released_states_are_never_acquirable(self) -> FgisCsAcquisitionPolicy:
        forbidden = {
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }
        if forbidden.intersection(self.allowed_project_states):
            raise ValueError("Released or historical projects cannot acquire FGIS CS evidence")
        return self


class StoredFgisCsExchange(DomainModel):
    request_uri: str = Field(pattern=r"^https://fgiscs\.minstroyrf\.ru/api/")
    response_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_object_key: str = Field(min_length=1, max_length=1000)
    response_size_bytes: int = Field(gt=0)


class FgisCsAcquisitionArtifact(DomainModel):
    schema_version: str
    request_context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: FgisCsMaterialLookupRequest
    result: FgisCsMaterialLookupResult
    exchanges: tuple[StoredFgisCsExchange, ...]

    @model_validator(mode="after")
    def artifact_is_complete_and_fail_closed(self) -> FgisCsAcquisitionArtifact:
        if self.schema_version != FGIS_CS_ACQUISITION_ARTIFACT_SCHEMA:
            raise ValueError("Unsupported FGIS CS acquisition artifact schema")
        if len(self.exchanges) != 4:
            raise ValueError("FGIS CS acquisition artifact must contain exactly four exchanges")
        if self.result.ready_for_pricing:
            raise ValueError("Raw FGIS CS acquisition cannot be ready for pricing")
        return self


class PreparedFgisCsAcquisition(DomainModel):
    artifact: FgisCsAcquisitionArtifact
    artifact_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_object_key: str = Field(min_length=1, max_length=1000)
    artifact_size_bytes: int = Field(gt=0)


class FgisCsAcquisitionRequestContext(DomainModel):
    project_id: str = Field(min_length=1, max_length=64)
    project_state: ApprovalState
    project_row_version: int = Field(gt=0)
    item_id: str = Field(min_length=1, max_length=128)
    boq_line_id: str = Field(min_length=1, max_length=64)
    boq_item_name: str = Field(min_length=1, max_length=4000)
    boq_unit: str = Field(min_length=1, max_length=64)
    nomenclature_match_id: str = Field(min_length=1, max_length=64)
    nomenclature_match_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_item_id: str = Field(min_length=1, max_length=128)
    document_set_revision_id: str = Field(min_length=1, max_length=64)
    document_set_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version_id: str = Field(min_length=1, max_length=64)
    policy_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_qualification_id: str = Field(min_length=1, max_length=128)
    adapter_test_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lookup_request: FgisCsMaterialLookupRequest
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FgisCsAcquisitionView(DomainModel):
    acquisition_id: str
    project_id: str
    item_id: str
    boq_item_name: str
    boq_unit: str
    nomenclature_match_id: str
    canonical_item_id: str
    document_set_revision_id: str
    policy_version_id: str
    adapter_qualification_id: str
    status: VerificationStatus
    basis_current: bool
    ready_for_pricing: bool = False
    pricing_blockers: tuple[str, ...]
    artifact_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact: FgisCsAcquisitionArtifact

    @model_validator(mode="after")
    def view_is_never_released_as_price(self) -> FgisCsAcquisitionView:
        if self.status is not VerificationStatus.UNVERIFIED or self.ready_for_pricing:
            raise ValueError("FGIS CS acquisition view must remain unverified and blocked")
        if not self.pricing_blockers:
            raise ValueError("FGIS CS acquisition view must expose pricing blockers")
        return self


class FgisCsAcquisitionListView(DomainModel):
    project_id: str
    item_id: str
    boq_item_name: str
    boq_unit: str
    acquisitions: tuple[FgisCsAcquisitionView, ...]
    release_warning: str = (
        "FGIS CS public values are source evidence only. They are not normalized prices "
        "and cannot be released without approved mapping and commercial-basis review."
    )


def prepare_fgiscs_material_acquisition(
    *,
    api: FgisCsPublicApi,
    request: FgisCsMaterialLookupRequest,
    expected_context_hash: str,
    object_store: ObjectStore,
    max_response_bytes: int,
) -> PreparedFgisCsAcquisition:
    """Acquire and persist raw source bodies without holding a database transaction."""

    if max_response_bytes <= 0:
        raise ValueError("FGIS CS response limit must be positive")
    if len(expected_context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_context_hash
    ):
        raise ValueError("FGIS CS request context hash is invalid")
    acquisition = api.acquire_material(request)
    stored_exchanges: list[StoredFgisCsExchange] = []
    for exchange in acquisition.exchanges:
        stored = object_store.put(
            BytesIO(exchange.response_body),
            max_bytes=max_response_bytes,
        )
        if stored.object_hash != exchange.response_sha256 or stored.size_bytes != len(
            exchange.response_body
        ):
            raise RuntimeError("Stored FGIS CS response identity does not reproduce")
        stored_exchanges.append(_stored_exchange(exchange=exchange, stored=stored))
    artifact = FgisCsAcquisitionArtifact(
        schema_version=FGIS_CS_ACQUISITION_ARTIFACT_SCHEMA,
        request_context_hash=expected_context_hash,
        request=acquisition.request,
        result=acquisition.result,
        exchanges=tuple(stored_exchanges),
    )
    artifact_bytes = canonical_json(artifact)
    stored_artifact = object_store.put(
        BytesIO(artifact_bytes),
        max_bytes=len(artifact_bytes),
    )
    return PreparedFgisCsAcquisition(
        artifact=artifact,
        artifact_object_hash=stored_artifact.object_hash,
        artifact_object_key=stored_artifact.object_key,
        artifact_size_bytes=stored_artifact.size_bytes,
    )


class FgisCsAcquisitionService:
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

    def request_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        resource_code: str,
        lock: bool = False,
    ) -> FgisCsAcquisitionRequestContext:
        if actor.roles != frozenset({ActorRole.SYSTEM}):
            raise ValueError("FGIS CS acquisition requires the isolated SYSTEM worker")
        if not self.settings.fgiscs_adapter_configured:
            raise ValueError("FGIS CS worker binding is not configured")
        if actor.actor_id != self.settings.fgiscs_worker_actor_id:
            raise ValueError("FGIS CS request differs from the configured worker binding")
        project = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            lock=lock,
            system_access=SystemProjectAccess(
                qualification_id=str(self.settings.fgiscs_adapter_qualification_id),
                capability=EvidenceMethod.EXTERNAL_SYSTEM.value,
            ),
        )
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        policy_row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            organization_id=project.organization_id,
            purpose=FGIS_CS_ACQUISITION_POLICY_PURPOSE,
            kind=FGIS_CS_ACQUISITION_POLICY_KIND,
        )
        policy = self._policy(policy_row.payload)
        if project.state not in policy.allowed_project_states:
            raise ValueError("FGIS CS acquisition is not allowed in the current project state")
        qualification = self._qualification(actor=actor, policy=policy)
        pricing = PricingService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        match = pricing.require_verified_nomenclature_match(
            project_id=project.id,
            item_id=item_id,
        )
        source_item = pricing.require_current_nomenclature_source_item(
            project_id=project.id,
            item_id=item_id,
        )
        if match.canonical_item_id is None:
            raise ValueError("Verified nomenclature match lacks a canonical item")
        lookup_request = FgisCsMaterialLookupRequest(
            subject_name=policy.subject_name,
            price_zone_name=policy.price_zone_name,
            period_name=policy.period_name,
            resource_code=resource_code,
        )
        basis = {
            "project_id": project.id,
            "project_row_version": project.row_version,
            "project_state": project.state,
            "item_id": item_id,
            "boq_source_item": source_item,
            "nomenclature_match_id": match.id,
            "nomenclature_match_hash": content_hash(match.payload),
            "document_set_revision_id": document_set.id,
            "document_set_manifest_hash": document_set.manifest_hash,
            "policy_version_id": policy_row.id,
            "policy_content_hash": policy_row.content_hash,
            "adapter_qualification_id": qualification.id,
            "adapter_test_evidence_hash": qualification.test_evidence_hash,
            "lookup_request": lookup_request,
        }
        return FgisCsAcquisitionRequestContext(
            project_id=project.id,
            project_state=project.state,
            project_row_version=project.row_version,
            item_id=item_id,
            boq_line_id=source_item.boq_line_id,
            boq_item_name=source_item.description,
            boq_unit=source_item.unit,
            nomenclature_match_id=match.id,
            nomenclature_match_hash=content_hash(match.payload),
            canonical_item_id=match.canonical_item_id,
            document_set_revision_id=document_set.id,
            document_set_manifest_hash=document_set.manifest_hash,
            policy_version_id=policy_row.id,
            policy_content_hash=policy_row.content_hash,
            adapter_qualification_id=qualification.id,
            adapter_test_evidence_hash=qualification.test_evidence_hash,
            lookup_request=lookup_request,
            context_hash=content_hash(basis),
        )

    def record_stored_acquisition(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        resource_code: str,
        expected_context_hash: str,
        prepared: PreparedFgisCsAcquisition,
        request_id: str,
        reason: str,
    ) -> FgisCsAcquisitionView:
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 2000:
            raise ValueError("FGIS CS acquisition reason must contain 1 to 2000 characters")
        context = self.request_context(
            actor=actor,
            project_id=project_id,
            item_id=item_id,
            resource_code=resource_code,
            lock=True,
        )
        if expected_context_hash != context.context_hash:
            raise ValueError("FGIS CS acquisition context changed while evidence was retrieved")
        if prepared.artifact.request != context.lookup_request:
            raise ValueError("Stored FGIS CS request differs from the governed request")
        if prepared.artifact.request_context_hash != expected_context_hash:
            raise ValueError("Stored FGIS CS artifact is not bound to the request context")
        artifact = self._replay_prepared(prepared)
        blockers = list(artifact.result.pricing_blockers)
        if artifact.result.price is None:
            blockers.append("FGIS_PRICE_NOT_FOUND")
        blockers = list(dict.fromkeys(blockers))
        identity = {
            "project_id": project_id,
            "item_id": item_id,
            "context_hash": context.context_hash,
            "artifact_object_hash": prepared.artifact_object_hash,
        }
        acquisition_id = f"fgiscs-acquisition-{content_hash(identity)[:24]}"
        payload = {
            "context": context.model_dump(mode="json"),
            "artifact_object_hash": prepared.artifact_object_hash,
            "artifact_object_key": prepared.artifact_object_key,
            "artifact_size_bytes": prepared.artifact_size_bytes,
            "artifact_hash": content_hash(artifact),
            "pricing_blockers": blockers,
            "ready_for_pricing": False,
        }
        existing = self.session.get(FgisCsAcquisitionRow, acquisition_id)
        if existing is not None:
            self._require_existing(existing=existing, context=context, payload=payload)
            return self._view(existing, context=context, artifact=artifact)
        now = utc_now()
        row = FgisCsAcquisitionRow(
            id=acquisition_id,
            project_id=project_id,
            item_id=item_id,
            nomenclature_match_id=context.nomenclature_match_id,
            document_set_revision_id=context.document_set_revision_id,
            policy_version_id=context.policy_version_id,
            adapter_qualification_id=context.adapter_qualification_id,
            status=VerificationStatus.UNVERIFIED.value,
            artifact_object_hash=prepared.artifact_object_hash,
            artifact_object_key=prepared.artifact_object_key,
            artifact_size_bytes=prepared.artifact_size_bytes,
            acquired_at=artifact.result.retrieved_at,
            payload=payload,
            created_at=now,
        )
        self.session.add(row)
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="fgiscs_source_evidence_acquired",
            actor=actor,
            request_id=request_id,
            reason=normalized_reason,
            payload={
                "acquisition_id": acquisition_id,
                "item_id": item_id,
                "nomenclature_match_id": context.nomenclature_match_id,
                "document_set_revision_id": context.document_set_revision_id,
                "policy_version_id": context.policy_version_id,
                "adapter_qualification_id": context.adapter_qualification_id,
                "artifact_object_hash": prepared.artifact_object_hash,
                "status": VerificationStatus.UNVERIFIED.value,
                "pricing_blockers": blockers,
            },
        )
        return self._view(row, context=context, artifact=artifact)

    def list_for_item(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        limit: int = 20,
    ) -> FgisCsAcquisitionListView:
        if limit < 1 or limit > 100:
            raise ValueError("FGIS CS acquisition limit must be between 1 and 100")
        project = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
            ),
        )
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        policy_row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            organization_id=project.organization_id,
            purpose=FGIS_CS_ACQUISITION_POLICY_PURPOSE,
            kind=FGIS_CS_ACQUISITION_POLICY_KIND,
        )
        pricing = PricingService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        match = pricing.require_verified_nomenclature_match(
            project_id=project_id,
            item_id=item_id,
        )
        source_item = pricing.require_current_nomenclature_source_item(
            project_id=project_id,
            item_id=item_id,
        )
        qualification = (
            self.session.get(
                AdapterQualificationRow,
                self.settings.fgiscs_adapter_qualification_id,
            )
            if self.settings.fgiscs_adapter_qualification_id is not None
            else None
        )
        rows = tuple(
            self.session.scalars(
                select(FgisCsAcquisitionRow)
                .where(
                    FgisCsAcquisitionRow.project_id == project_id,
                    FgisCsAcquisitionRow.item_id == item_id,
                )
                .order_by(FgisCsAcquisitionRow.acquired_at.desc(), FgisCsAcquisitionRow.id)
                .limit(limit)
            )
        )
        views: list[FgisCsAcquisitionView] = []
        for row in rows:
            context = FgisCsAcquisitionRequestContext.model_validate(row.payload.get("context"))
            artifact = self._replay_row(row)
            basis_current = (
                row.nomenclature_match_id == match.id
                and context.nomenclature_match_hash == content_hash(match.payload)
                and context.boq_line_id == source_item.boq_line_id
                and context.boq_item_name == source_item.description
                and context.boq_unit == source_item.unit
                and context.project_state == project.state
                and context.project_row_version == project.row_version
                and row.document_set_revision_id == document_set.id
                and context.document_set_manifest_hash == document_set.manifest_hash
                and row.policy_version_id == policy_row.id
                and context.policy_content_hash == policy_row.content_hash
                and qualification is not None
                and qualification.status == "APPROVED"
                and row.adapter_qualification_id == qualification.id
                and context.adapter_test_evidence_hash == qualification.test_evidence_hash
                and (
                    qualification.valid_until is None
                    or qualification.valid_until >= utc_now().date()
                )
            )
            views.append(
                self._view(
                    row,
                    context=context,
                    artifact=artifact,
                    basis_current=basis_current,
                )
            )
        return FgisCsAcquisitionListView(
            project_id=project_id,
            item_id=item_id,
            boq_item_name=source_item.description,
            boq_unit=source_item.unit,
            acquisitions=tuple(views),
        )

    @staticmethod
    def _policy(payload: dict[str, object]) -> FgisCsAcquisitionPolicy:
        return FgisCsAcquisitionPolicy.model_validate(
            {
                "subject_name": payload.get("subject_name"),
                "price_zone_name": payload.get("price_zone_name"),
                "period_name": payload.get("period_name"),
                "allowed_project_states": payload.get("allowed_project_states"),
                "required_adapter_name": payload.get("required_adapter_name"),
                "required_adapter_version": payload.get("required_adapter_version"),
                "source_origin_id": payload.get("source_origin_id"),
            }
        )

    def _qualification(
        self,
        *,
        actor: Actor,
        policy: FgisCsAcquisitionPolicy,
    ) -> AdapterQualificationRow:
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == self.settings.fgiscs_adapter_qualification_id,
                AdapterQualificationRow.status == "APPROVED",
                AdapterQualificationRow.adapter_name == policy.required_adapter_name,
                AdapterQualificationRow.adapter_version == policy.required_adapter_version,
            )
        )
        if qualification is None:
            raise ValueError("FGIS CS adapter qualification does not match the policy")
        if (
            qualification.adapter_name != self.settings.fgiscs_adapter
            or actor.actor_id != self.settings.fgiscs_worker_actor_id
        ):
            raise ValueError("FGIS CS adapter differs from the configured worker binding")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("FGIS CS adapter qualification has expired")
        payload = qualification.payload
        if (
            payload.get("organization_id") != actor.organization_id
            or payload.get("service_actor_id") != actor.actor_id
            or EvidenceMethod.EXTERNAL_SYSTEM.value not in payload.get("supported_methods", [])
            or "FGIS_CS" not in payload.get("supported_price_source_types", [])
            or policy.source_origin_id not in payload.get("supported_price_source_origins", [])
        ):
            raise ValueError("FGIS CS service identity or source scope is not qualified")
        return qualification

    def _replay_prepared(
        self,
        prepared: PreparedFgisCsAcquisition,
    ) -> FgisCsAcquisitionArtifact:
        manifest_content = self._read_object(
            object_hash=prepared.artifact_object_hash,
            expected_size=prepared.artifact_size_bytes,
        )
        if canonical_json(prepared.artifact) != manifest_content:
            raise RuntimeError("FGIS CS artifact manifest does not match its stored object")
        return self._replay_artifact(prepared.artifact)

    def _replay_row(self, row: FgisCsAcquisitionRow) -> FgisCsAcquisitionArtifact:
        if row.status != VerificationStatus.UNVERIFIED.value:
            raise RuntimeError("Stored FGIS CS acquisition status is invalid")
        manifest_content = self._read_object(
            object_hash=row.artifact_object_hash,
            expected_size=row.artifact_size_bytes,
        )
        artifact = FgisCsAcquisitionArtifact.model_validate_json(manifest_content)
        if canonical_json(artifact) != manifest_content:
            raise RuntimeError("Stored FGIS CS artifact manifest is not canonical")
        if row.payload.get("artifact_hash") != content_hash(artifact):
            raise RuntimeError("Stored FGIS CS artifact hash does not match its row")
        return self._replay_artifact(artifact)

    def _replay_artifact(
        self,
        artifact: FgisCsAcquisitionArtifact,
    ) -> FgisCsAcquisitionArtifact:
        raw_exchanges: list[FgisCsRawHttpExchange] = []
        for exchange in artifact.exchanges:
            body = self._read_object(
                object_hash=exchange.response_object_hash,
                expected_size=exchange.response_size_bytes,
            )
            raw_exchanges.append(
                FgisCsRawHttpExchange(
                    request_uri=exchange.request_uri,
                    response_body=body,
                )
            )
        acquisition = FgisCsMaterialAcquisition(
            request=artifact.request,
            result=artifact.result,
            exchanges=tuple(raw_exchanges),
        )
        replay_fgiscs_material_acquisition(acquisition)
        return artifact

    def _read_object(self, *, object_hash: str, expected_size: int) -> bytes:
        if expected_size <= 0:
            raise RuntimeError("FGIS CS evidence object size is invalid")
        with self.object_store.open(object_hash) as stream:
            content = stream.read(expected_size + 1)
        if len(content) != expected_size:
            raise RuntimeError("FGIS CS evidence object size does not match its manifest")
        if hashlib.sha256(content).hexdigest() != object_hash:
            raise RuntimeError("FGIS CS evidence object hash does not match its manifest")
        return content

    @staticmethod
    def _require_existing(
        *,
        existing: FgisCsAcquisitionRow,
        context: FgisCsAcquisitionRequestContext,
        payload: dict[str, object],
    ) -> None:
        if (
            existing.project_id != context.project_id
            or existing.item_id != context.item_id
            or existing.nomenclature_match_id != context.nomenclature_match_id
            or existing.document_set_revision_id != context.document_set_revision_id
            or existing.policy_version_id != context.policy_version_id
            or existing.adapter_qualification_id != context.adapter_qualification_id
            or existing.status != VerificationStatus.UNVERIFIED.value
            or existing.payload != payload
            or ensure_utc(existing.acquired_at) is None
            or ensure_utc(existing.created_at) is None
        ):
            raise RuntimeError("Existing FGIS CS acquisition fails integrity replay")

    @staticmethod
    def _view(
        row: FgisCsAcquisitionRow,
        *,
        context: FgisCsAcquisitionRequestContext,
        artifact: FgisCsAcquisitionArtifact,
        basis_current: bool = True,
    ) -> FgisCsAcquisitionView:
        if (
            row.project_id != context.project_id
            or row.item_id != context.item_id
            or row.nomenclature_match_id != context.nomenclature_match_id
            or row.document_set_revision_id != context.document_set_revision_id
            or row.policy_version_id != context.policy_version_id
            or row.adapter_qualification_id != context.adapter_qualification_id
            or row.artifact_object_hash != row.payload.get("artifact_object_hash")
            or row.artifact_object_key != row.payload.get("artifact_object_key")
            or row.artifact_size_bytes != row.payload.get("artifact_size_bytes")
            or ensure_utc(row.acquired_at) != artifact.result.retrieved_at
        ):
            raise RuntimeError("FGIS CS acquisition row identity does not reproduce")
        raw_blockers = row.payload.get("pricing_blockers")
        expected_blockers = list(artifact.result.pricing_blockers)
        if artifact.result.price is None:
            expected_blockers.append("FGIS_PRICE_NOT_FOUND")
        expected_blockers = list(dict.fromkeys(expected_blockers))
        if (
            not isinstance(raw_blockers, list)
            or raw_blockers != expected_blockers
            or not all(
                isinstance(blocker, str)
                and blocker
                and blocker == blocker.strip()
                and len(blocker) <= 200
                and not any(character in blocker for character in "\r\n\x00")
                for blocker in raw_blockers
            )
            or row.payload.get("ready_for_pricing") is not False
        ):
            raise RuntimeError("FGIS CS acquisition blocker evidence does not reproduce")
        blockers = list(raw_blockers)
        if not basis_current:
            blockers.append("ACQUISITION_BASIS_IS_NOT_CURRENT")
        return FgisCsAcquisitionView(
            acquisition_id=row.id,
            project_id=row.project_id,
            item_id=row.item_id,
            boq_item_name=context.boq_item_name,
            boq_unit=context.boq_unit,
            nomenclature_match_id=row.nomenclature_match_id,
            canonical_item_id=context.canonical_item_id,
            document_set_revision_id=row.document_set_revision_id,
            policy_version_id=row.policy_version_id,
            adapter_qualification_id=row.adapter_qualification_id,
            status=VerificationStatus(row.status),
            basis_current=basis_current,
            pricing_blockers=tuple(dict.fromkeys(blockers)),
            artifact_object_hash=row.artifact_object_hash,
            artifact=artifact,
        )


def _stored_exchange(
    *,
    exchange: FgisCsRawHttpExchange,
    stored: StoredObject,
) -> StoredFgisCsExchange:
    return StoredFgisCsExchange(
        request_uri=exchange.request_uri,
        response_object_hash=stored.object_hash,
        response_object_key=stored.object_key,
        response_size_bytes=stored.size_bytes,
    )
