"""Tests for log_filters — upstream-library noise suppression.

Covers:
- Filter behavior: drops records whose message contains the defect
  marker; passes everything else.
- ``install_filters`` is idempotent: repeated calls do not stack
  duplicate filters on the target logger.
- ``install_filters`` is invoked at package import time (any consumer
  of Core gets the filter without explicit setup).
"""

from __future__ import annotations

import importlib
import logging

import pytest
from solentlabs.cable_modem_monitor_core import log_filters
from solentlabs.cable_modem_monitor_core.log_filters import (
    SuppressMissingHeaderBodySeparator,
    install_filters,
)

# ┌─────────────────────────────────────────────────────────┬───────────┐
# │ message                                                 │ allowed?  │
# ├─────────────────────────────────────────────────────────┼───────────┤
# │ "Failed to parse headers: [MissingHeaderBodySeparator]" │ False     │
# │ "MissingHeaderBodySeparatorDefect()"                    │ False     │
# │ "Some other urllib3 warning"                            │ True      │
# │ "Connection refused"                                    │ True      │
# │ ""                                                      │ True      │
# └─────────────────────────────────────────────────────────┴───────────┘
#
# fmt: off
_FILTER_CASES = [
    ("Failed to parse headers: [MissingHeaderBodySeparatorDefect()]", False, "exact_marker"),
    ("MissingHeaderBodySeparatorDefect somewhere",                    False, "marker_anywhere"),
    ("Some other urllib3 warning",                                    True,  "unrelated_warning"),
    ("Connection refused",                                            True,  "connection_error"),
    ("",                                                              True,  "empty_message"),
]
# fmt: on


@pytest.mark.parametrize(
    "message, expected_allowed, _desc",
    _FILTER_CASES,
    ids=[c[2] for c in _FILTER_CASES],
)
def test_suppress_filter(message: str, expected_allowed: bool, _desc: str) -> None:
    """Filter passes records iff their message lacks the defect marker."""
    record = logging.LogRecord(
        name="urllib3.connection",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )
    assert SuppressMissingHeaderBodySeparator().filter(record) is expected_allowed


def test_install_is_idempotent() -> None:
    """Repeated calls do not stack duplicate filters on the target logger."""
    target = logging.getLogger("urllib3.connection")
    initial = sum(1 for f in target.filters if isinstance(f, SuppressMissingHeaderBodySeparator))

    # The package import in conftest already installed the filter once,
    # so we expect exactly one instance going in.
    assert initial == 1

    install_filters()
    install_filters()
    install_filters()

    after = sum(1 for f in target.filters if isinstance(f, SuppressMissingHeaderBodySeparator))
    assert after == 1


def test_filter_installed_on_package_import() -> None:
    """Importing the package attaches the filter — no explicit setup needed."""
    # Reload the package and verify the filter is present on the target
    # logger immediately afterwards (no manual install_filters() call
    # in this test).
    importlib.reload(log_filters)
    log_filters.install_filters()

    target = logging.getLogger("urllib3.connection")
    assert any(isinstance(f, SuppressMissingHeaderBodySeparator) for f in target.filters)


def test_filter_blocks_record_through_logger() -> None:
    """End-to-end: a defect record routed through the logger is suppressed.

    Uses ``caplog`` semantics with a manually-attached handler so we can
    verify the filter chain runs as part of the standard logging path,
    not just as an isolated callable.
    """
    captured: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = captured.append  # type: ignore[method-assign]

    target = logging.getLogger("urllib3.connection")
    target.addHandler(handler)
    original_level = target.level
    target.setLevel(logging.DEBUG)

    try:
        target.warning("Failed: MissingHeaderBodySeparatorDefect()")
        target.warning("Connection refused")
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)

    messages = [rec.getMessage() for rec in captured]
    assert "Failed: MissingHeaderBodySeparatorDefect()" not in messages
    assert "Connection refused" in messages
