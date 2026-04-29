"""Suppress upstream-library log noise that obscures CMM signal.

Some library WARNING records emit full tracebacks for conditions that
are not actionable for CMM contributors debugging modem behavior — they
look like unhandled exceptions but are caught internally by the
library that emitted them. Each filter below drops one specific known
pattern.

Filters are installed automatically when the ``cable_modem_monitor_core``
package is imported (see ``__init__.py``), so any consumer — HA
adapter, test harness, catalog tools, ad-hoc scripts — gets the same
clean log surface without explicit setup.

Adding a new filter:

1. Identify the logger name and a stable substring in the record
   message (the message-text marker is what we filter on; logger name
   tells us where to attach).
2. Add a ``Filter`` subclass below with a docstring explaining the
   source — which modem firmware, which library version, what
   triggers it, why it is harmless.
3. Register it in :func:`install_filters`.

Filters here are pure suppression. If a future case needs translation
("emit a single CMM-level note instead of dropping silently"), extend
the filter to log its own DEBUG-level record before returning False.
"""

from __future__ import annotations

import logging

_URLLIB3_CONNECTION_LOGGER = "urllib3.connection"


class SuppressMissingHeaderBodySeparator(logging.Filter):
    """Drop urllib3 ``MissingHeaderBodySeparatorDefect`` warnings.

    Source: ARRIS SB6141 firmware (and possibly other older modems)
    returns headers with a space before the colon — e.g.
    ``Cache-Control : no-cache`` instead of ``Cache-Control: no-cache``.
    Python 3.14+ urllib3 calls ``assert_header_parsing`` on every
    response and raises ``HeaderParsingError`` for the malformed
    header. urllib3 catches the exception internally and emits a
    WARNING with a full traceback — the response body parses fine, so
    nothing is actually broken, but the traceback looks alarming and
    fires once per HTTP request.

    Filtering is exact: the substring ``MissingHeaderBodySeparatorDefect``
    only appears in this specific defect's records, so other urllib3
    warnings are unaffected.
    """

    _NEEDLE = "MissingHeaderBodySeparatorDefect"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._NEEDLE not in record.getMessage()


def install_filters() -> None:
    """Install all CMM logging filters. Idempotent — safe to call repeatedly.

    Filters are attached to the loggers they target. Each filter type
    is added at most once per logger; subsequent calls re-check
    presence so reloading the package (or calling explicitly from a
    test fixture) does not stack duplicate filters.
    """
    _ensure_filter(_URLLIB3_CONNECTION_LOGGER, SuppressMissingHeaderBodySeparator)


def _ensure_filter(
    logger_name: str,
    filter_cls: type[logging.Filter],
) -> None:
    """Attach ``filter_cls`` to the named logger if not already present."""
    target = logging.getLogger(logger_name)
    for existing in target.filters:
        if isinstance(existing, filter_cls):
            return
    target.addFilter(filter_cls())
