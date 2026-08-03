from __future__ import annotations

import json

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
