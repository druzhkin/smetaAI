from __future__ import annotations

import json

from tenderguard import cli
from tenderguard.application.automation_rework import (
    AutomationDispatchDisposition,
    AutomationDispatchResult,
)
from tenderguard.config import Settings


def test_dispatch_cli_fails_closed_without_worker_binding(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(app_env="test"))

    assert cli.dispatch_final_rework(1) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "detail": "automatic rework worker binding is not configured",
    }


def test_dispatch_cli_rejects_unbounded_batch_before_opening_database(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must not be loaded")),
    )

    assert cli.dispatch_final_rework(10_001) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "detail": "max-events must be between 1 and 10000",
    }


def test_dispatch_cli_reports_queued_command_without_calling_it_completed(
    monkeypatch,
    capsys,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        automation_rework_adapter="qualified-automation-dispatcher",
        automation_rework_qualification_id="qualification-automation-v1",
        automation_rework_worker_actor_id="automation-worker",
    )

    class _Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class _Dispatcher:
        calls = 0

        def __init__(self, **kwargs) -> None:
            assert kwargs["settings"] is settings

        def dispatch_next(self, *, worker_id: str) -> AutomationDispatchResult:
            assert worker_id.startswith("automation-rework-worker-")
            self.calls += 1
            if self.calls == 1:
                return AutomationDispatchResult(
                    disposition=AutomationDispatchDisposition.STAGE_COMMAND_QUEUED
                )
            return AutomationDispatchResult(disposition=AutomationDispatchDisposition.IDLE)

    engine = _Engine()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database_engine", lambda configured: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda configured: object())
    monkeypatch.setattr(cli, "build_object_store", lambda configured: object())
    monkeypatch.setattr(
        "tenderguard.application.automation_rework.AutomationReworkDispatcher",
        _Dispatcher,
    )

    assert cli.dispatch_final_rework(2) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "COMMANDS_QUEUED"
    assert payload["counts"]["STAGE_COMMAND_QUEUED"] == 1
    assert payload["counts"]["IDLE"] == 1
    assert engine.disposed is True


def test_dispatch_cli_fails_closed_and_disposes_engine_when_qualification_is_invalid(
    monkeypatch,
    capsys,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        automation_rework_adapter="qualified-automation-dispatcher",
        automation_rework_qualification_id="qualification-automation-v1",
        automation_rework_worker_actor_id="automation-worker",
    )

    class _Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class _Dispatcher:
        def __init__(self, **kwargs) -> None:
            assert kwargs["settings"] is settings

        def dispatch_next(self, *, worker_id: str) -> AutomationDispatchResult:
            assert worker_id.startswith("automation-rework-worker-")
            raise ValueError("worker qualification is invalid")

    engine = _Engine()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database_engine", lambda configured: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda configured: object())
    monkeypatch.setattr(cli, "build_object_store", lambda configured: object())
    monkeypatch.setattr(
        "tenderguard.application.automation_rework.AutomationReworkDispatcher",
        _Dispatcher,
    )

    assert cli.dispatch_final_rework(1) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "detail": "worker qualification is invalid",
    }
    assert engine.disposed is True


def test_dispatch_cli_returns_attention_required_for_blocked_dispatch(
    monkeypatch,
    capsys,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        automation_rework_adapter="qualified-automation-dispatcher",
        automation_rework_qualification_id="qualification-automation-v1",
        automation_rework_worker_actor_id="automation-worker",
    )

    class _Engine:
        def dispose(self) -> None:
            return None

    class _Dispatcher:
        def __init__(self, **kwargs) -> None:
            assert kwargs["settings"] is settings

        def dispatch_next(self, *, worker_id: str) -> AutomationDispatchResult:
            assert worker_id.startswith("automation-rework-worker-")
            return AutomationDispatchResult(disposition=AutomationDispatchDisposition.BLOCKED)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database_engine", lambda configured: _Engine())
    monkeypatch.setattr(cli, "create_session_factory", lambda configured: object())
    monkeypatch.setattr(cli, "build_object_store", lambda configured: object())
    monkeypatch.setattr(
        "tenderguard.application.automation_rework.AutomationReworkDispatcher",
        _Dispatcher,
    )

    assert cli.dispatch_final_rework(1) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ATTENTION_REQUIRED"
    assert payload["counts"]["BLOCKED"] == 1
