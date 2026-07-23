from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from tenderguard.config import Settings
from tenderguard.infrastructure.intake import inspect_intake
from tenderguard.infrastructure.object_store import LocalObjectStore


def test_excel_inspection_reports_hidden_content_and_uncalculated_formulas() -> None:
    workbook = Workbook()
    visible = workbook.active
    visible.title = "BoQ"
    visible["A1"] = 10
    visible["A2"] = 20
    visible["A3"] = "=SUM(A1:A2)"
    visible.row_dimensions[2].hidden = True
    visible.column_dimensions["B"].hidden = True
    hidden = workbook.create_sheet("Hidden assumptions")
    hidden.sheet_state = "hidden"
    hidden["A1"] = "manual coefficient"
    output = BytesIO()
    workbook.save(output)

    manifest = inspect_intake("boq.xlsx", output.getvalue(), Settings(app_env="test"))
    codes = {finding.code for finding in manifest.findings}
    assert "HIDDEN_EXCEL_SHEET" in codes
    assert "HIDDEN_EXCEL_DIMENSIONS" in codes
    assert "EXCEL_FORMULA_CACHE_MISSING" in codes
    assert not manifest.all_files_processed
    assert {sheet.name for sheet in manifest.entries[0].sheets} == {
        "BoQ",
        "Hidden assumptions",
    }


def test_archive_path_traversal_is_blocked() -> None:
    payload = BytesIO()
    with ZipFile(payload, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("../outside.txt", "unsafe")
        archive.writestr("safe.txt", "safe")
    manifest = inspect_intake("package.zip", payload.getvalue(), Settings(app_env="test"))
    assert not manifest.all_files_processed
    assert any(finding.code == "ARCHIVE_PATH_TRAVERSAL" for finding in manifest.findings)
    assert any(entry.archive_path.endswith("safe.txt") for entry in manifest.entries)


def test_local_object_store_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    first = store.put(BytesIO(b"same content"))
    second = store.put(BytesIO(b"same content"))
    assert first == second
    with store.open(first.object_hash) as stream:
        assert stream.read() == b"same content"
    stored_files = [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
    assert len(stored_files) == 1
    stored_files[0].write_bytes(b"tampered content")
    with (
        pytest.raises(RuntimeError, match="fails SHA-256 verification"),
        store.open(first.object_hash),
    ):
        pass
