from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tenderguard import cli
from tenderguard.config import Settings
from tenderguard.integrations.fgiscs_public import FgisCsPublicApiError

_PERIOD_NAME = "2 квартал 2026 г."  # noqa: RUF001


def test_ksr_probe_emits_unverified_result(
    monkeypatch,
    capsys,
) -> None:
    class _Result:
        def model_dump_json(self, *, indent: int) -> str:
            assert indent == 2
            return '{"status":"UNVERIFIED"}'

    monkeypatch.setattr(
        cli.FgisCsPublicApi,
        "search_ksr",
        lambda self, query: _Result(),
    )

    assert cli.probe_fgiscs_ksr("песок природный") == 0
    assert json.loads(capsys.readouterr().out) == {"status": "UNVERIFIED"}


def test_material_probe_passes_exact_request_without_approving_price(
    monkeypatch,
    capsys,
) -> None:
    captured = {}

    class _Result:
        def model_dump_json(self, *, indent: int) -> str:
            assert indent == 2
            return '{"ready_for_pricing":false}'

    def lookup(self, request):
        captured["request"] = request
        return _Result()

    monkeypatch.setattr(cli.FgisCsPublicApi, "lookup_material", lookup)

    assert (
        cli.probe_fgiscs_material(
            subject_name="Московская область",
            price_zone_name=None,
            period_name=_PERIOD_NAME,
            resource_code="02.3.01.02-1102",
        )
        == 0
    )
    request = captured["request"]
    assert request.subject_name == "Московская область"
    assert request.price_zone_name is None
    assert request.period_name == _PERIOD_NAME
    assert request.resource_code == "02.3.01.02-1102"
    assert json.loads(capsys.readouterr().out) == {"ready_for_pricing": False}


def test_probe_fails_closed_with_machine_readable_error(
    monkeypatch,
    capsys,
) -> None:
    def fail(self, query):
        raise FgisCsPublicApiError(
            code="FGIS_HTTP_429",
            retryable=True,
        )

    monkeypatch.setattr(cli.FgisCsPublicApi, "search_ksr", fail)

    assert cli.probe_fgiscs_ksr("песок") == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "FGIS_HTTP_429",
        "retryable": True,
    }


def test_material_probe_rejects_multiline_source_identity_before_network(capsys) -> None:
    assert (
        cli.probe_fgiscs_material(
            subject_name="Moscow region\nspoof",
            price_zone_name=None,
            period_name="2026 Q2",
            resource_code="02.3.01.02-1102",
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "FGIS_REQUEST_INVALID",
        "retryable": False,
    }


def test_material_probe_publishes_raw_diagnostic_package_atomically(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    class _Manifest:
        ready_for_pricing = False

        def model_dump(self, *, mode: str):
            assert mode == "python"
            return {"status": "UNVERIFIED", "ready_for_pricing": False}

        def model_dump_json(self, *, indent: int) -> str:
            assert indent == 2
            return '{"status":"UNVERIFIED","ready_for_pricing":false}'

    acquisition = object()
    prepared = SimpleNamespace(
        manifest=_Manifest(),
        raw_files=(
            ("raw/01-" + "a" * 64 + ".json", b"{}"),
            ("raw/02-" + "b" * 64 + ".json", b"[]"),
        ),
    )
    monkeypatch.setattr(
        cli.FgisCsPublicApi,
        "acquire_material",
        lambda _self, _request: acquisition,
    )
    monkeypatch.setattr(
        "tenderguard.application.fgiscs_diagnostic.prepare_fgiscs_diagnostic_material_package",
        lambda supplied: prepared if supplied is acquisition else None,
    )

    output_dir = tmp_path / "fgis-package"
    assert (
        cli.probe_fgiscs_material(
            subject_name="Московская область",
            price_zone_name=None,
            period_name=_PERIOD_NAME,
            resource_code="02.3.01.02-1102",
            output_dir=output_dir,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ready_for_pricing"] is False
    assert json.loads((output_dir / "manifest.json").read_text(encoding="utf-8")) == {
        "ready_for_pricing": False,
        "status": "UNVERIFIED",
    }
    assert (output_dir / prepared.raw_files[0][0]).read_bytes() == b"{}"

    assert (
        cli.probe_fgiscs_material(
            subject_name="Московская область",
            price_zone_name=None,
            period_name=_PERIOD_NAME,
            resource_code="02.3.01.02-1102",
            output_dir=output_dir,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["code"] == ("FGIS_DIAGNOSTIC_OUTPUT_ALREADY_EXISTS")

    original_write = cli._write_bytes_exclusive

    def fail_manifest(destination: Path, payload: bytes) -> None:
        if destination.name == "manifest.json":
            raise OSError("simulated manifest failure")
        original_write(destination, payload)

    monkeypatch.setattr(cli, "_write_bytes_exclusive", fail_manifest)
    failed_output = tmp_path / "failed-fgis-package"
    assert (
        cli.probe_fgiscs_material(
            subject_name="Московская область",
            price_zone_name=None,
            period_name=_PERIOD_NAME,
            resource_code="02.3.01.02-1102",
            output_dir=failed_output,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["code"] == ("FGIS_DIAGNOSTIC_PACKAGE_WRITE_FAILED")
    assert not failed_output.exists()
    assert not tuple(tmp_path.glob(".failed-fgis-package.staging-*"))


def test_governed_import_fails_before_network_without_complete_worker_binding(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(app_env="test"),
    )

    assert (
        cli.import_governed_fgiscs_material(
            project_id="project-1",
            item_id="item-1",
            resource_code="02.3.01.02-1102",
            request_id="request-1",
            reason="Acquire exact source evidence",
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "code": "FGIS_CS_WORKER_NOT_CONFIGURED",
        "retryable": False,
        "ready_for_pricing": False,
    }
