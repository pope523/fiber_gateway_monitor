"""Tests for the ``run_restart`` one-shot command.

Covers the procedure defined in ORCHESTRATION_SPEC.md § Restart
Action: authentication → action execution → session clear → recovery
window. The only emitted error token is ``"command_failed"``.

Use case coverage:
- UC-40: Restart dispatches and opens a recovery window.
- UC-42: Back-to-back restarts are allowed by Core (serialization
  is the consumer's responsibility; see HA_ADAPTER_SPEC § Operation
  Mutex).
- UC-44: ``actions.restart`` absent → ``RestartNotSupportedError``.
- UC-45: Dispatch bypasses the auth circuit breaker (tested in
  test_orchestrator.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    HttpAction,
)
from solentlabs.cable_modem_monitor_core.orchestration.recovery import Recovery
from solentlabs.cable_modem_monitor_core.orchestration.restart import (
    RestartNotSupportedError,
    run_restart,
)


def _config(*, has_restart: bool = True) -> MagicMock:
    """Build a mock ModemConfig."""
    config = MagicMock()
    config.model = "T100"
    config.timeout = 10
    if has_restart:
        config.actions.restart = HttpAction(
            type="http",
            method="POST",
            endpoint="/restart.htm",
            params={"restart": "1"},
        )
        config.actions.logout = None
    else:
        config.actions = None
    return config


def _collector(auth_success: bool = True) -> MagicMock:
    """Build a mock ModemDataCollector with cooperative defaults."""
    collector = MagicMock()
    collector._session = MagicMock()
    collector._base_url = "http://192.168.100.1"
    collector.authenticate.return_value.success = auth_success
    collector.authenticate.return_value.error = "" if auth_success else "wrong"
    return collector


def _recovery(config: MagicMock, collector: MagicMock) -> Recovery:
    """Build a real Recovery instance bound to the mocks."""
    return Recovery(collector=collector, modem_config=config)


# ------------------------------------------------------------------
# Not-supported guard
# ------------------------------------------------------------------


def test_raises_when_restart_action_absent() -> None:
    config = _config(has_restart=False)
    collector = _collector()
    recovery = _recovery(config, collector)

    with pytest.raises(RestartNotSupportedError):
        run_restart(collector, config, recovery)


# ------------------------------------------------------------------
# Success path
# ------------------------------------------------------------------


def test_success_opens_recovery_window() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config, collector)

    result = run_restart(collector, config, recovery)

    assert result.success is True
    assert result.error == ""
    assert result.elapsed_seconds >= 0
    assert recovery.active is True


def test_success_clears_session_once() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config, collector)

    run_restart(collector, config, recovery)

    # One clear_session call — between action execution and recovery.begin.
    assert collector.clear_session.call_count == 1


def test_success_authenticates_and_executes_action() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config, collector)

    run_restart(collector, config, recovery)

    collector.authenticate.assert_called_once()
    collector._session.request.assert_called_once()


# ------------------------------------------------------------------
# Failure paths — error token must be "command_failed" only
# ------------------------------------------------------------------


def test_auth_failure_returns_command_failed() -> None:
    config = _config()
    collector = _collector(auth_success=False)
    recovery = _recovery(config, collector)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_action_exception_returns_command_failed() -> None:
    config = _config()
    collector = _collector()
    collector._session.request.side_effect = RuntimeError("boom")
    recovery = _recovery(config, collector)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_clear_session_exception_returns_command_failed() -> None:
    config = _config()
    collector = _collector()
    collector.clear_session.side_effect = RuntimeError("broken")
    recovery = _recovery(config, collector)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


# ------------------------------------------------------------------
# Re-entrancy — UC-42 retired behavior
# ------------------------------------------------------------------


def test_restart_while_recovery_active_is_allowed() -> None:
    """Core does not arbitrate. A second restart during a recovery
    window dispatches normally — the caller may want a retry.
    """
    config = _config()
    collector = _collector()
    recovery = _recovery(config, collector)

    result_first = run_restart(collector, config, recovery)
    assert result_first.success is True
    assert recovery.active is True

    # Window is open; caller presses restart again — must not raise.
    result_second = run_restart(collector, config, recovery)
    assert result_second.success is True
    assert recovery.active is True
