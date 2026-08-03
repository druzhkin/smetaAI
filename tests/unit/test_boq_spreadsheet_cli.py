from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

from tenderguard import cli
from tenderguard.config import Settings


def _workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "BoQ"
    worksheet.append(["ID", "Description", "Unit", "Quantity"])
    worksheet.append([1, "Cable", "m", 10])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _profile(workbook_sha256: str) -> dict[str, object]:
    return {
        "schema_version": "boq-xlsx-profile/v1",
        "profile_version_id": "cli-test-v1",
        "worksheet_name": "BoQ",
        "header_row": 1,
        "data_start_row": 2,
        "data_end_row": 2,
        "position_id": {"column": 1, "header": "ID"},
        "description": {"column": 2, "header": "Description"},
        "unit": {"column": 3, "header": "Unit"},
        "quantity": {"column": 4, "header": "Quantity"},
        "position_id_pattern": r"^\d+$",
        "section_row_patterns": [],
        "allowed_units": ["m"],
        "quantity_decimal_separator": ".",
        "allow_quantity_formulas": False,
        "expected_workbook_sha256": workbook_sha256,
    }


def test_probe_boq_xlsx_reads_standalone_workbook(
    tmp_path: Path,
    capsys,
) -> None:
    content = _workbook()
    source = tmp_path / "source.xlsx"
    profile = tmp_path / "profile.json"
    source.write_bytes(content)
    profile.write_text(
        json.dumps(_profile(hashlib.sha256(content).hexdigest())),
        encoding="utf-8",
    )

    exit_code = cli.probe_boq_xlsx(
        input_path=source,
        profile_path=profile,
        archive_entry_sha256=None,
        output_path=None,
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "UNVERIFIED"
    assert result["ready_for_boq"] is False
    assert result["candidates"][0]["cells"]["description"]["coordinate"] == "B2"


def test_probe_boq_xlsx_selects_archive_member_by_hash(
    tmp_path: Path,
    capsys,
) -> None:
    content = _workbook()
    workbook_sha256 = hashlib.sha256(content).hexdigest()
    source = tmp_path / "source.zip"
    profile = tmp_path / "profile.json"
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("documents/source.xlsx", content)
        archive.writestr("readme.txt", "supporting file")
    profile.write_text(
        json.dumps(_profile(workbook_sha256)),
        encoding="utf-8",
    )

    exit_code = cli.probe_boq_xlsx(
        input_path=source,
        profile_path=profile,
        archive_entry_sha256=None,
        output_path=None,
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["archive_path"] == "source.zip!/documents/source.xlsx"
    assert result["workbook_object_sha256"] == workbook_sha256


def test_probe_boq_xlsx_fails_closed_on_invalid_profile(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "source.xlsx"
    profile = tmp_path / "profile.json"
    source.write_bytes(_workbook())
    profile.write_text('{"schema_version":"unknown"}', encoding="utf-8")

    exit_code = cli.probe_boq_xlsx(
        input_path=source,
        profile_path=profile,
        archive_entry_sha256=None,
        output_path=None,
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "BOQ_PROBE_INPUT_INVALID",
        "ready_for_boq": False,
    }


def test_probe_output_is_exclusive_and_never_overwrites(
    tmp_path: Path,
    capsys,
) -> None:
    content = _workbook()
    source = tmp_path / "source.xlsx"
    profile = tmp_path / "profile.json"
    output = tmp_path / "result.json"
    source.write_bytes(content)
    profile.write_text(
        json.dumps(_profile(hashlib.sha256(content).hexdigest())),
        encoding="utf-8",
    )

    assert (
        cli.probe_boq_xlsx(
            input_path=source,
            profile_path=profile,
            archive_entry_sha256=None,
            output_path=output,
        )
        == 0
    )
    first_payload = output.read_text(encoding="utf-8")
    assert json.loads(first_payload)["status"] == "UNVERIFIED"
    capsys.readouterr()

    assert (
        cli.probe_boq_xlsx(
            input_path=source,
            profile_path=profile,
            archive_entry_sha256=None,
            output_path=output,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "BOQ_OUTPUT_ALREADY_EXISTS",
        "ready_for_boq": False,
    }
    assert output.read_text(encoding="utf-8") == first_payload


def test_governed_import_fails_closed_without_worker_binding(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(app_env="test"),
    )

    exit_code = cli.import_governed_boq_xlsx(
        project_id="project-1",
        document_revision_id="revision-1",
        request_id="request-1",
        reason="controlled import test",
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "BOQ_XLSX_WORKER_NOT_CONFIGURED",
        "ready_for_boq": False,
    }
