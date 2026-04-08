"""Form field discovery — extract input fields from login page HTML.

**Build-time utility** for the MCP intake pipeline. Parses login page
HTML from a HAR capture to discover hidden form fields, which are then
written into the generated modem.yaml ``hidden_fields`` config.

This is NOT used at runtime by ``FormAuthManager``. The auth manager
sends exactly what modem.yaml declares — no runtime HTML parsing.
Keeping field discovery at build time ensures the YAML is the complete,
intentional declaration of what gets POSTed.
"""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup, Tag

_logger = logging.getLogger(__name__)


def extract_form_fields(
    html: str,
    form_selector: str = "",
) -> dict[str, str]:
    """Extract input fields from an HTML login page.

    Finds the target ``<form>`` element and returns all ``<input>``
    and ``<select>`` fields that have a ``name`` attribute, along
    with their default values.

    Args:
        html: Raw HTML string (login page body).
        form_selector: CSS selector to target a specific ``<form>``.
            If empty, uses the first ``<form>`` found. If no
            ``<form>`` exists, falls back to all ``<input>`` elements
            in the page.

    Returns:
        Dict of field name to default value. Empty dict if no fields
        found or HTML cannot be parsed.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    scope = _find_form_scope(soup, form_selector)
    if scope is None:
        return {}

    fields: dict[str, str] = {}
    _collect_inputs(scope, fields)
    _collect_selects(scope, fields)

    _logger.debug("Form discovery: %d fields extracted", len(fields))
    return fields


def _find_form_scope(
    soup: BeautifulSoup,
    form_selector: str,
) -> Tag | BeautifulSoup | None:
    """Find the HTML scope to search for form fields.

    Priority:
    1. CSS selector match (if ``form_selector`` is non-empty)
    2. First ``<form>`` element
    3. Entire page (fallback for pages without ``<form>`` tags)

    Returns ``None`` only if the page has no input elements at all.
    """
    if form_selector:
        match = soup.select_one(form_selector)
        if match is not None:
            return match
        _logger.debug("Form selector '%s' matched nothing, falling back", form_selector)

    form = soup.find("form")
    if form is not None:
        return form

    # No <form> element — check if bare <input> tags exist anywhere.
    if soup.find("input"):
        _logger.debug("No <form> element found, using page-level fallback")
        return soup

    return None


def _collect_inputs(scope: Any, fields: dict[str, str]) -> None:
    """Collect all ``<input>`` elements with a ``name`` attribute."""
    for tag in scope.find_all("input"):
        name = tag.get("name")
        if name:
            fields[name] = tag.get("value", "")


def _collect_selects(scope: Any, fields: dict[str, str]) -> None:
    """Collect ``<select>`` elements — use the selected option's value."""
    for tag in scope.find_all("select"):
        name = tag.get("name")
        if not name:
            continue

        # Find the selected option, fall back to first option.
        selected = tag.find("option", selected=True)
        if selected is None:
            selected = tag.find("option")

        if selected is not None:
            fields[name] = selected.get("value", selected.get_text(strip=True))
