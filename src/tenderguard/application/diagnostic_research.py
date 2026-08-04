from __future__ import annotations

# ruff: noqa: RUF001 -- Russian operator-facing text is intentional.
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from tenderguard.application.boq_fgis_history import (
    BoqFgisHistoryPackage,
    PreparedBoqFgisHistoryPackage,
    verify_boq_fgis_history_package,
)
from tenderguard.application.boq_market_assessment import (
    BoqMarketTechnicalAssessmentPackage,
    verify_boq_market_technical_assessment,
)
from tenderguard.application.boq_market_research import (
    BoqMarketResearchPackage,
    PreparedBoqMarketResearchPackage,
    verify_boq_market_research_package,
)
from tenderguard.application.free_source_research import (
    BoqFreeSourceResearchLine,
    BoqFreeSourceResearchResult,
    PreparedBoqFreeSourceResearch,
    verify_boq_free_source_research_package,
)
from tenderguard.application.pricing import (
    BoqDiagnosticObservedAmountView,
    BoqDiagnosticResearchRouteView,
    BoqDiagnosticSourceCandidateView,
)
from tenderguard.domain.boq_spreadsheet import BoqXlsxExtractionResult
from tenderguard.domain.common import content_hash
from tenderguard.domain.models import DomainModel
from tenderguard.integrations.fgiscs_public import FgisCsMaterialHistoryObservation


class HashPinnedDiagnosticArtifact(DomainModel):
    path: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("Diagnostic artifact path must be normalized")
        return value


class DiagnosticResearchReferences(DomainModel):
    free_source_research: HashPinnedDiagnosticArtifact
    fgis_history: HashPinnedDiagnosticArtifact | None = None
    market_research: HashPinnedDiagnosticArtifact | None = None
    market_assessment: HashPinnedDiagnosticArtifact | None = None

    @model_validator(mode="after")
    def assessment_requires_market_package(self) -> DiagnosticResearchReferences:
        if self.market_assessment is not None and self.market_research is None:
            raise ValueError("Diagnostic market assessment requires its market package")
        paths = tuple(
            item.path
            for item in (
                self.free_source_research,
                self.fgis_history,
                self.market_research,
                self.market_assessment,
            )
            if item is not None
        )
        if len(paths) != len(set(paths)):
            raise ValueError("Diagnostic research references contain duplicate paths")
        return self


@dataclass(frozen=True)
class DiagnosticResearchBundle:
    free_source: BoqFreeSourceResearchResult
    free_source_manifest_sha256: str
    fgis_history: BoqFgisHistoryPackage | None = None
    fgis_history_manifest_sha256: str | None = None
    market_research: BoqMarketResearchPackage | None = None
    market_research_manifest_sha256: str | None = None
    market_assessment: BoqMarketTechnicalAssessmentPackage | None = None
    market_assessment_manifest_sha256: str | None = None

    @property
    def generated_at(self) -> datetime:
        timestamps = [self.free_source.completed_at]
        if self.fgis_history is not None:
            timestamps.append(self.fgis_history.history.retrieved_at)
        if self.market_research is not None:
            timestamps.append(self.market_research.completed_at)
        if self.market_assessment is not None:
            timestamps.append(self.market_assessment.completed_at)
        return max(timestamps)

    @property
    def package_hashes(self) -> dict[str, str]:
        result = {"free_source_research": self.free_source_manifest_sha256}
        for key, value in (
            ("fgis_history", self.fgis_history_manifest_sha256),
            ("market_research", self.market_research_manifest_sha256),
            ("market_assessment", self.market_assessment_manifest_sha256),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class DiagnosticRowResearch:
    route: BoqDiagnosticResearchRouteView
    fgis_candidates: tuple[BoqDiagnosticSourceCandidateView, ...]
    market_candidates: tuple[BoqDiagnosticSourceCandidateView, ...]
    blockers: tuple[str, ...]


class DiagnosticResearchLoadError(RuntimeError):
    pass


def load_diagnostic_research(
    *,
    references: DiagnosticResearchReferences,
    base_directory: Path,
    extraction: BoqXlsxExtractionResult,
    maximum_file_bytes: int,
) -> DiagnosticResearchBundle:
    try:
        free_source, free_payload, _ = _load_free_source(
            references.free_source_research,
            base_directory=base_directory,
            maximum_file_bytes=maximum_file_bytes,
        )
        if (
            free_source.result.extraction_content_hash != content_hash(extraction)
            or free_source.result.workbook_sha256 != extraction.workbook_object_sha256
        ):
            raise ValueError("Diagnostic research is not bound to the loaded extraction")
        free_hash = hashlib.sha256(free_payload).hexdigest()

        fgis_manifest = None
        fgis_hash = None
        if references.fgis_history is not None:
            fgis_manifest, fgis_payload = _load_fgis_history(
                references.fgis_history,
                base_directory=base_directory,
                maximum_file_bytes=maximum_file_bytes,
            )
            fgis_hash = hashlib.sha256(fgis_payload).hexdigest()
            if (
                fgis_manifest.research_manifest_sha256 != free_hash
                or fgis_manifest.research_result_content_hash
                != content_hash(free_source.result)
            ):
                raise ValueError("Diagnostic FGIS history is not bound to free-source research")

        market_manifest = None
        market_prepared = None
        market_payload = None
        market_hash = None
        if references.market_research is not None:
            market_prepared, market_payload = _load_market_research(
                references.market_research,
                base_directory=base_directory,
                maximum_file_bytes=maximum_file_bytes,
            )
            market_manifest = market_prepared.manifest
            market_hash = hashlib.sha256(market_payload).hexdigest()
            if (
                market_manifest.research_manifest_sha256 != free_hash
                or market_manifest.research_result_content_hash
                != content_hash(free_source.result)
            ):
                raise ValueError("Diagnostic market package is not bound to free-source research")

        assessment = None
        assessment_hash = None
        if references.market_assessment is not None:
            assert market_prepared is not None and market_payload is not None
            assessment, assessment_payload = _load_market_assessment(
                references.market_assessment,
                base_directory=base_directory,
                maximum_file_bytes=maximum_file_bytes,
            )
            assessment_hash = hashlib.sha256(assessment_payload).hexdigest()
            verify_boq_market_technical_assessment(
                market=market_prepared,
                source_market_manifest_sha256=hashlib.sha256(market_payload).hexdigest(),
                assessment=assessment,
            )

        workbook_hashes = {
            free_source.result.workbook_sha256,
            *(
                (fgis_manifest.workbook_sha256,)
                if fgis_manifest is not None
                else ()
            ),
            *(
                (market_manifest.workbook_sha256,)
                if market_manifest is not None
                else ()
            ),
            *((assessment.workbook_sha256,) if assessment is not None else ()),
        }
        if workbook_hashes != {extraction.workbook_object_sha256}:
            raise ValueError("Diagnostic research packages refer to different workbooks")
        return DiagnosticResearchBundle(
            free_source=free_source.result,
            free_source_manifest_sha256=free_hash,
            fgis_history=fgis_manifest,
            fgis_history_manifest_sha256=fgis_hash,
            market_research=market_manifest,
            market_research_manifest_sha256=market_hash,
            market_assessment=assessment,
            market_assessment_manifest_sha256=assessment_hash,
        )
    except DiagnosticResearchLoadError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        raise DiagnosticResearchLoadError(str(error)) from error


def build_diagnostic_row_research(
    bundle: DiagnosticResearchBundle,
) -> dict[str, DiagnosticRowResearch]:
    fgis_by_line = _build_fgis_candidates(bundle)
    market_by_line = _build_market_candidates(bundle)
    fgis_line_results = (
        {item.candidate_id: item for item in bundle.fgis_history.line_results}
        if bundle.fgis_history is not None
        else {}
    )
    market_line_results = (
        {item.candidate_id: item for item in bundle.market_research.line_results}
        if bundle.market_research is not None
        else {}
    )
    result: dict[str, DiagnosticRowResearch] = {}
    for line in bundle.free_source.lines:
        blockers = list(line.pricing_blockers)
        fgis_line = fgis_line_results.get(line.candidate_id)
        if fgis_line is not None:
            blockers.extend(fgis_line.pricing_blockers)
        market_line = market_line_results.get(line.candidate_id)
        if market_line is not None:
            blockers.extend(market_line.pricing_blockers)
        route_blockers = tuple(dict.fromkeys(blockers))
        route = BoqDiagnosticResearchRouteView(
            cost_nature=line.cost_nature,
            pricing_route={
                "WORK": "NORMATIVE_ENGINE",
                "MATERIAL": "FGIS_AND_MARKET",
                "LOGISTICS": "LOGISTICS_MODEL",
            }[line.cost_nature],
            profile_version_id=bundle.free_source.profile_version_id,
            profile_content_hash=bundle.free_source.profile_content_hash,
            rationale=_route_rationale(line),
            blockers=route_blockers,
        )
        result[line.candidate_id] = DiagnosticRowResearch(
            route=route,
            fgis_candidates=fgis_by_line.get(line.candidate_id, ()),
            market_candidates=market_by_line.get(line.candidate_id, ()),
            blockers=route_blockers,
        )
    return result


def _load_free_source(
    reference: HashPinnedDiagnosticArtifact,
    *,
    base_directory: Path,
    maximum_file_bytes: int,
) -> tuple[PreparedBoqFreeSourceResearch, bytes, Path]:
    payload, manifest_path = _read_hash_pinned(
        reference,
        base_directory=base_directory,
        maximum_file_bytes=maximum_file_bytes,
        label="free-source research manifest",
    )
    result = BoqFreeSourceResearchResult.model_validate_json(payload)
    raw_responses = tuple(
        (
            artifact.sha256,
            _read_package_child(
                manifest_path.parent,
                Path("raw") / f"{artifact.sha256}.json",
                maximum_file_bytes=maximum_file_bytes,
            ),
        )
        for artifact in result.raw_artifacts
    )
    prepared = PreparedBoqFreeSourceResearch(result=result, raw_responses=raw_responses)
    verify_boq_free_source_research_package(prepared)
    return prepared, payload, manifest_path


def _load_fgis_history(
    reference: HashPinnedDiagnosticArtifact,
    *,
    base_directory: Path,
    maximum_file_bytes: int,
) -> tuple[BoqFgisHistoryPackage, bytes]:
    payload, manifest_path = _read_hash_pinned(
        reference,
        base_directory=base_directory,
        maximum_file_bytes=maximum_file_bytes,
        label="FGIS history manifest",
    )
    manifest = BoqFgisHistoryPackage.model_validate_json(payload)
    raw_files = tuple(
        (
            item.file_name,
            _read_package_child(
                manifest_path.parent,
                Path(item.file_name),
                maximum_file_bytes=maximum_file_bytes,
            ),
        )
        for item in manifest.raw_responses
    )
    verify_boq_fgis_history_package(
        PreparedBoqFgisHistoryPackage(manifest=manifest, raw_files=raw_files)
    )
    return manifest, payload


def _load_market_research(
    reference: HashPinnedDiagnosticArtifact,
    *,
    base_directory: Path,
    maximum_file_bytes: int,
) -> tuple[PreparedBoqMarketResearchPackage, bytes]:
    payload, manifest_path = _read_hash_pinned(
        reference,
        base_directory=base_directory,
        maximum_file_bytes=maximum_file_bytes,
        label="market research manifest",
    )
    manifest = BoqMarketResearchPackage.model_validate_json(payload)
    raw_files = tuple(
        (
            item.file_name,
            _read_package_child(
                manifest_path.parent,
                Path(item.file_name),
                maximum_file_bytes=maximum_file_bytes,
            ),
        )
        for item in manifest.raw_responses
    )
    prepared = PreparedBoqMarketResearchPackage(manifest=manifest, raw_files=raw_files)
    verify_boq_market_research_package(prepared)
    return prepared, payload


def _load_market_assessment(
    reference: HashPinnedDiagnosticArtifact,
    *,
    base_directory: Path,
    maximum_file_bytes: int,
) -> tuple[BoqMarketTechnicalAssessmentPackage, bytes]:
    payload, _ = _read_hash_pinned(
        reference,
        base_directory=base_directory,
        maximum_file_bytes=maximum_file_bytes,
        label="market assessment manifest",
    )
    return BoqMarketTechnicalAssessmentPackage.model_validate_json(payload), payload


def _read_hash_pinned(
    reference: HashPinnedDiagnosticArtifact,
    *,
    base_directory: Path,
    maximum_file_bytes: int,
    label: str,
) -> tuple[bytes, Path]:
    raw_path = Path(reference.path)
    try:
        resolved = (
            raw_path if raw_path.is_absolute() else base_directory / raw_path
        ).resolve(strict=True)
    except OSError as error:
        raise DiagnosticResearchLoadError(f"Diagnostic {label} does not exist") from error
    payload = _read_bounded(resolved, maximum_file_bytes=maximum_file_bytes, label=label)
    actual_hash = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual_hash, reference.sha256):
        raise DiagnosticResearchLoadError(
            f"Diagnostic {label} differs from the hash-pinned project manifest"
        )
    return payload, resolved


def _read_package_child(
    package_directory: Path,
    relative_path: Path,
    *,
    maximum_file_bytes: int,
) -> bytes:
    if relative_path.is_absolute():
        raise ValueError("Diagnostic package child path must be relative")
    try:
        resolved_base = package_directory.resolve(strict=True)
        resolved = (resolved_base / relative_path).resolve(strict=True)
        resolved.relative_to(resolved_base)
    except (OSError, ValueError) as error:
        raise ValueError("Diagnostic package child path escapes its package") from error
    return _read_bounded(
        resolved,
        maximum_file_bytes=maximum_file_bytes,
        label="diagnostic raw evidence",
    )


def _read_bounded(path: Path, *, maximum_file_bytes: int, label: str) -> bytes:
    size = path.stat().st_size
    if size <= 0 or size > maximum_file_bytes:
        raise ValueError(f"{label.capitalize()} size is invalid")
    payload = path.read_bytes()
    if len(payload) != size:
        raise ValueError(f"{label.capitalize()} changed while loading")
    return payload


def _route_rationale(line: BoqFreeSourceResearchLine) -> tuple[str, ...]:
    if line.cost_nature == "WORK":
        return (
            "Строка классифицирована как работа в зафиксированном профиле исследования.",
            "Основной маршрут — нормативный расчёт; без утверждённого движка "
            "стоимость не формируется.",
        )
    if line.cost_nature == "LOGISTICS":
        return (
            "Строка классифицирована как логистика в зафиксированном профиле исследования.",
            "Стоимость требует доказуемого маршрута, тарифа и утверждённой логистической модели.",
        )
    return (
        "Строка классифицирована как материал в зафиксированном профиле исследования.",
        "Кандидаты ФГИС ЦС и рынка показаны только для проверки наименований и исходных данных.",
    )


def _build_fgis_candidates(
    bundle: DiagnosticResearchBundle,
) -> dict[str, tuple[BoqDiagnosticSourceCandidateView, ...]]:
    if bundle.fgis_history is None:
        return {}
    free_lines = {line.candidate_id: line for line in bundle.free_source.lines}
    observations_by_code: dict[str, list[FgisCsMaterialHistoryObservation]] = {}
    for observation in bundle.fgis_history.history.observations:
        observations_by_code.setdefault(observation.requested_resource_code, []).append(
            observation
        )
    result: dict[str, tuple[BoqDiagnosticSourceCandidateView, ...]] = {}
    for line_result in bundle.fgis_history.line_results:
        free_line = free_lines[line_result.candidate_id]
        search_candidates = {
            item.resource_code: item
            for item in (
                free_line.fgis_search_result.candidates
                if free_line.fgis_search_result is not None
                else ()
            )
        }
        candidates: list[BoqDiagnosticSourceCandidateView] = []
        for resource_code in line_result.resource_codes:
            ksr_candidate = search_candidates[resource_code]
            published = tuple(
                observation
                for observation in observations_by_code.get(resource_code, [])
                if observation.price is not None
            )
            if published:
                for observation in published:
                    assert observation.price is not None
                    price = observation.price
                    candidates.append(
                        _fgis_candidate(
                            bundle=bundle,
                            line=free_line,
                            line_blockers=line_result.pricing_blockers,
                            source_item_name=price.source_item_name,
                            source_unit=price.unit,
                            source_record_id=price.source_record_id,
                            resource_code=resource_code,
                            source_locator=observation.api_request_uri,
                            evidence_sha256=observation.response_sha256,
                            observed_at=bundle.fgis_history.history.retrieved_at,
                            period_name=observation.period.name,
                            candidate_content_hash=content_hash(price),
                            observed_amounts=(
                                BoqDiagnosticObservedAmountView(
                                    amount_kind="FGIS_AGGREGATED",
                                    amount=price.aggregated_price,
                                    amount_literal=price.source_amount_literals[
                                        "aggregatedPrice"
                                    ],
                                    unit=price.unit,
                                ),
                                BoqDiagnosticObservedAmountView(
                                    amount_kind="FGIS_ESTIMATED",
                                    amount=price.estimated_price,
                                    amount_literal=price.source_amount_literals[
                                        "estimatedPrice"
                                    ],
                                    unit=price.unit,
                                ),
                                BoqDiagnosticObservedAmountView(
                                    amount_kind="FGIS_DISTANCE",
                                    amount=price.distance_price,
                                    amount_literal=price.source_amount_literals[
                                        "distancePrice"
                                    ],
                                    unit=price.unit,
                                ),
                            ),
                            attributes={
                                "Код КСР": resource_code,
                                "Субъект": bundle.fgis_history.history.subject.name,
                                "Ценовая зона": bundle.fgis_history.history.price_zone.name,
                                "Заготовительно-складские расходы, %": (
                                    price.source_amount_literals[
                                        "procureStorageCostPercent"
                                    ]
                                ),
                            },
                            extra_blockers=observation.pricing_blockers,
                        )
                    )
            else:
                assert free_line.fgis_search_result is not None
                assert free_line.raw_response_sha256 is not None
                candidates.append(
                    _fgis_candidate(
                        bundle=bundle,
                        line=free_line,
                        line_blockers=line_result.pricing_blockers,
                        source_item_name=ksr_candidate.source_item_name,
                        source_unit=ksr_candidate.unit,
                        source_record_id=ksr_candidate.source_record_id,
                        resource_code=resource_code,
                        source_locator=free_line.fgis_search_result.api_request_uri,
                        evidence_sha256=free_line.raw_response_sha256,
                        observed_at=free_line.fgis_search_result.retrieved_at,
                        period_name=None,
                        candidate_content_hash=content_hash(ksr_candidate),
                        observed_amounts=(),
                        attributes={
                            "Код КСР": resource_code,
                            "Субъект": bundle.fgis_history.history.subject.name,
                            "Ценовая зона": bundle.fgis_history.history.price_zone.name,
                        },
                    )
                )
        result[line_result.candidate_id] = tuple(candidates)
    return result


def _fgis_candidate(
    *,
    bundle: DiagnosticResearchBundle,
    line: BoqFreeSourceResearchLine,
    line_blockers: tuple[str, ...],
    source_item_name: str,
    source_unit: str,
    source_record_id: str,
    resource_code: str,
    source_locator: str,
    evidence_sha256: str,
    observed_at: datetime,
    period_name: str | None,
    candidate_content_hash: str,
    observed_amounts: tuple[BoqDiagnosticObservedAmountView, ...],
    attributes: dict[str, str],
    extra_blockers: tuple[str, ...] = (),
) -> BoqDiagnosticSourceCandidateView:
    assert bundle.fgis_history is not None
    name_equal = line.boq_description == source_item_name
    unit_equal = line.boq_unit == source_unit
    boq_only = tuple(
        item
        for item, equal in (
            (f"NAME:{line.boq_description}", name_equal),
            (f"UNIT:{line.boq_unit}", unit_equal),
        )
        if not equal
    )
    source_only = tuple(
        item
        for item, equal in (
            (f"NAME:{source_item_name}", name_equal),
            (f"UNIT:{source_unit}", unit_equal),
        )
        if not equal
    )
    blockers = tuple(
        dict.fromkeys(
            (
                *line_blockers,
                *extra_blockers,
                "DIAGNOSTIC_SOURCE_CANDIDATE_NOT_PRICE_EVIDENCE",
            )
        )
    )
    seed = {
        "candidate_id": line.candidate_id,
        "resource_code": resource_code,
        "source_record_id": source_record_id,
        "period_name": period_name,
        "evidence_sha256": evidence_sha256,
    }
    return BoqDiagnosticSourceCandidateView(
        research_id=f"diagnostic-fgis-{content_hash(seed)[:24]}",
        source_group="FGIS_CS",
        source_type="FGIS_CS",
        source_display_name="ФГИС ЦС — публичный портал",
        source_item_name=source_item_name,
        source_record_id=source_record_id,
        source_uri=bundle.fgis_history.history.public_page_uri,
        source_locator=source_locator,
        observed_at=observed_at,
        period_name=period_name,
        evidence_sha256=evidence_sha256,
        candidate_content_hash=candidate_content_hash,
        observed_amounts=observed_amounts,
        attributes=attributes,
        boq_only_literals=boq_only,
        source_only_literals=source_only,
        comparison_method=(
            "EXACT_LITERAL_NAME_AND_UNIT"
            if name_equal and unit_equal
            else "DIAGNOSTIC_LITERAL_NAME_AND_UNIT_COMPARISON"
        ),
        blockers=blockers,
    )


def _build_market_candidates(
    bundle: DiagnosticResearchBundle,
) -> dict[str, tuple[BoqDiagnosticSourceCandidateView, ...]]:
    if bundle.market_research is None or bundle.market_assessment is None:
        return {}
    market_lines = {line.candidate_id: line for line in bundle.market_research.line_results}
    result: dict[str, tuple[BoqDiagnosticSourceCandidateView, ...]] = {}
    for assessment_line in bundle.market_assessment.lines:
        market_line = market_lines[assessment_line.candidate_id]
        observations = {item.request.source_uri: item for item in market_line.sources}
        candidates: list[BoqDiagnosticSourceCandidateView] = []
        for assessment in assessment_line.candidate_assessments:
            observation = observations[assessment.source_request.source_uri]
            assert observation.page_result is not None
            assert observation.response_sha256 is not None
            candidate = assessment.candidate
            attributes: dict[str, str] = {
                "Метод извлечения": candidate.extraction_method,
            }
            for key, value in (
                ("Бренд", candidate.brand_name),
                ("Наличие на странице", candidate.availability_literal),
                ("Цена действует до", candidate.price_valid_until_literal),
                ("Единица на странице", candidate.unit_text or candidate.unit_code),
            ):
                if value is not None:
                    attributes[key] = value
            blockers = tuple(
                dict.fromkeys(
                    (
                        *assessment.commercial_gaps,
                        *assessment_line.blockers,
                        "DIAGNOSTIC_SOURCE_CANDIDATE_NOT_PRICE_EVIDENCE",
                    )
                )
            )
            seed = {
                "candidate_id": assessment_line.candidate_id,
                "source_uri": assessment.source_request.source_uri,
                "candidate_content_hash": assessment.candidate_content_hash,
            }
            candidates.append(
                BoqDiagnosticSourceCandidateView(
                    research_id=f"diagnostic-market-{content_hash(seed)[:24]}",
                    source_group="MARKET",
                    source_type=assessment.source_request.source_type.value,
                    source_display_name=assessment.source_request.display_name,
                    source_item_name=candidate.source_item_name,
                    source_record_id=candidate.source_record_locator,
                    source_uri=assessment.source_request.source_uri,
                    source_locator=candidate.source_path,
                    observed_at=observation.page_result.retrieved_at,
                    evidence_sha256=observation.response_sha256,
                    candidate_content_hash=assessment.candidate_content_hash,
                    observed_amounts=(
                        BoqDiagnosticObservedAmountView(
                            amount_kind="MARKET_OFFER",
                            amount=candidate.amount,
                            amount_literal=candidate.amount_literal,
                            currency=candidate.currency,
                            unit=candidate.unit_text or candidate.unit_code,
                        ),
                    ),
                    attributes=attributes,
                    boq_only_literals=(
                        assessment.literal_comparison.boq_only_literal_identities
                    ),
                    source_only_literals=(
                        assessment.literal_comparison.source_only_literal_identities
                    ),
                    comparison_method=assessment.literal_comparison.algorithm_version,
                    blockers=blockers,
                )
            )
        result[assessment_line.candidate_id] = tuple(candidates)
    return result
