from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook

from tenderguard.config import Settings
from tenderguard.domain.boq_spreadsheet import (
    BoqXlsxColumn,
    BoqXlsxProfile,
)
from tenderguard.infrastructure.boq_spreadsheet import (
    BoqXlsxExtractionError,
    extract_boq_xlsx_candidates,
)
from tenderguard.infrastructure.intake import inspect_intake

_SHEET_NAME = "ВОР"  # noqa: RUF001


def _profile(**updates: object) -> BoqXlsxProfile:
    payload: dict[str, object] = {
        "profile_version_id": "test-profile-v1",
        "worksheet_name": _SHEET_NAME,
        "header_row": 1,
        "data_start_row": 2,
        "data_end_row": 3,
        "position_id": {"column": 1, "header": "№"},
        "description": {"column": 2, "header": "Наименование"},
        "unit": {"column": 3, "header": "Ед."},
        "quantity": {"column": 4, "header": "Количество"},
        "reference": {"column": 5, "header": "Ссылка"},
        "position_id_pattern": r"^\d+(?:\.\d+)*$",
        "section_row_patterns": (r"^Раздел ",),
        "allowed_units": ("м", "шт"),
        "quantity_decimal_separator": ".",
        "allow_quantity_formulas": False,
    }
    payload.update(updates)
    return BoqXlsxProfile.model_validate(payload)


def _workbook_bytes(*, dangerous: bool = False) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = _SHEET_NAME
    worksheet.append(["№", "Наименование", "Ед.", "Количество", "Ссылка"])
    if dangerous:
        worksheet.append([1, "Кабель", "м", "=1+1", "Лист 1"])
        worksheet.append([1, "Муфта", "компл.", 2, "Лист 2"])
        worksheet.column_dimensions["F"].hidden = True
    else:
        worksheet.append([1, "Кабель", "м", 1.25, "Лист 1"])
        worksheet.append([2, "Муфта", "шт", 2, "Лист 2"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_extracts_unverified_rows_with_exact_cell_provenance() -> None:
    content = _workbook_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    manifest = inspect_intake(
        "source.xlsx",
        content,
        Settings(app_env="test"),
    )
    profile = _profile(expected_workbook_sha256=sha256)
    extracted_at = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    result = extract_boq_xlsx_candidates(
        workbook_content=content,
        workbook_archive_path="source.xlsx",
        manifest=manifest,
        profile=profile,
        extracted_at=extracted_at,
    )

    assert result.status == "UNVERIFIED"
    assert not result.ready_for_boq
    assert result.global_blockers == ()
    assert result.workbook_object_sha256 == sha256
    assert result.extracted_at == extracted_at
    assert len(result.candidates) == 2
    first = result.candidates[0]
    assert first.status.value == "UNVERIFIED"
    assert first.source_position_id == "1"
    assert first.description == "Кабель"
    assert first.unit == "м"
    assert first.quantity == Decimal("1.25")
    assert first.blockers == ()
    assert first.cells["position_id"].coordinate == "A2"
    assert first.cells["description"].source_literal == "Кабель"
    assert first.cells["quantity"].value_kind == "NUMBER"
    assert first.cells["quantity"].source_literal == "1.25"


def test_dangerous_content_is_visible_but_cannot_advance() -> None:
    content = _workbook_bytes(dangerous=True)
    manifest = inspect_intake(
        "dangerous.xlsx",
        content,
        Settings(app_env="test"),
    )

    result = extract_boq_xlsx_candidates(
        workbook_content=content,
        workbook_archive_path="dangerous.xlsx",
        manifest=manifest,
        profile=_profile(),
    )

    assert result.status == "BLOCKED"
    assert not result.ready_for_boq
    assert "INTAKE_MANIFEST_BLOCKED" in result.global_blockers
    assert "HIDDEN_WORKBOOK_CONTENT" in result.global_blockers
    assert "INTAKE_EXCEL_FORMULA_CACHE_MISSING" in result.global_blockers
    first, second = result.candidates
    assert first.quantity is None
    assert "QUANTITY_FORMULA_NOT_ALLOWED" in first.blockers
    assert "QUANTITY_FORMULA_CACHE_MISSING" in first.blockers
    assert "DUPLICATE_POSITION_ID" in first.blockers
    assert "UNIT_NOT_ALLOWED_BY_PROFILE" in second.blockers
    assert "DUPLICATE_POSITION_ID" in second.blockers


def test_formula_position_id_never_becomes_a_stable_identifier() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = _SHEET_NAME
    worksheet.append(["№", "Наименование", "Ед.", "Количество", "Ссылка"])
    worksheet.append(["=ROW()-1", "Кабель", "м", 10, "Лист 1"])
    worksheet.append([2, "Муфта", "шт", 2, "Лист 2"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    content = output.getvalue()
    manifest = inspect_intake("formula-id.xlsx", content, Settings(app_env="test"))

    result = extract_boq_xlsx_candidates(
        workbook_content=content,
        workbook_archive_path="formula-id.xlsx",
        manifest=manifest,
        profile=_profile(),
    )

    assert result.status == "BLOCKED"
    first = result.candidates[0]
    assert first.source_position_id is None
    assert "POSITION_ID_FORMULA_NOT_ALLOWED" in first.blockers
    assert "MISSING_STABLE_POSITION_ID" in first.blockers
    assert first.cells["position_id"].formula == "=ROW()-1"


def test_exact_profile_header_and_workbook_fingerprint_are_enforced() -> None:
    content = _workbook_bytes()
    manifest = inspect_intake("source.xlsx", content, Settings(app_env="test"))
    mismatched = _profile(
        description={"column": 2, "header": "Описание"},
    )
    with pytest.raises(BoqXlsxExtractionError) as header_error:
        extract_boq_xlsx_candidates(
            workbook_content=content,
            workbook_archive_path="source.xlsx",
            manifest=manifest,
            profile=mismatched,
        )
    assert header_error.value.code == "BOQ_HEADER_MISMATCH_DESCRIPTION"

    wrong_fingerprint = _profile(expected_workbook_sha256="0" * 64)
    with pytest.raises(BoqXlsxExtractionError) as fingerprint_error:
        extract_boq_xlsx_candidates(
            workbook_content=content,
            workbook_archive_path="source.xlsx",
            manifest=manifest,
            profile=wrong_fingerprint,
        )
    assert fingerprint_error.value.code == "BOQ_WORKBOOK_FINGERPRINT_MISMATCH"


def test_profile_rejects_ambiguous_columns_and_invalid_patterns() -> None:
    with pytest.raises(ValueError, match="semantic columns must be unique"):
        _profile(
            unit=BoqXlsxColumn(column=2, header="Ед."),
        )
    with pytest.raises(ValueError, match="Position ID pattern is invalid"):
        _profile(position_id_pattern="[")
