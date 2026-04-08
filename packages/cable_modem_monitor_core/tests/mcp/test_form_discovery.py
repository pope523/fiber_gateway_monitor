"""Tests for form field discovery — extract_form_fields()."""

from __future__ import annotations

from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.mcp.analysis.auth.form_discovery import (
    extract_form_fields,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "form_discovery"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# Table-driven: extract_form_fields()
# ---------------------------------------------------------------------------

# fmt: off
# (description, fixture_file, form_selector, expected)
EXTRACT_CASES = [
    (
        "single_form_hidden_fields",
        "single_form_hidden.html",
        "",
        {"tok": "x", "mode": "login"},
    ),
    (
        "text_and_password_inputs",
        "text_and_password.html",
        "",
        {"user": "", "pwd": ""},
    ),
    (
        "multiple_forms_no_selector",
        "multiple_forms.html",
        "",
        {"q": ""},  # first form wins
    ),
    (
        "multiple_forms_with_selector",
        "multiple_forms.html",
        "form#login",
        {"user": "admin"},
    ),
    (
        "selector_no_match_falls_back",
        "selector_no_match.html",
        "form#missing",
        {"a": "1"},  # falls back to first <form>
    ),
    (
        "no_form_bare_inputs",
        "bare_inputs.html",
        "",
        {"x": "y"},  # page-level fallback
    ),
    (
        "empty_html",
        "empty.html",
        "",
        {},
    ),
    (
        "no_inputs_at_all",
        "no_inputs.html",
        "",
        {},
    ),
    (
        "input_without_name",
        "input_without_name.html",
        "",
        {},
    ),
    (
        "input_without_value",
        "input_without_value.html",
        "",
        {"field": ""},
    ),
    (
        "select_element_selected",
        "select_element.html",
        "",
        {"lang": "en"},
    ),
    (
        "select_no_selected_option",
        "select_no_selected.html",
        "",
        {"lang": "en"},  # falls back to first option
    ),
    (
        "mixed_inputs_and_hidden",
        "mixed_inputs.html",
        "",
        {
            "login_user": "technician",
            "pws": "",
            "todo": "login",
            "this_file": "login.html",
            "language": "en",
            "passwd": "",
            "cur_passwd": "",
        },
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,fixture,form_selector,expected",
    EXTRACT_CASES,
    ids=[c[0] for c in EXTRACT_CASES],
)
def test_extract_form_fields(
    desc: str,
    fixture: str,
    form_selector: str,
    expected: dict[str, str],
) -> None:
    """extract_form_fields returns the expected field dict."""
    html = _load(fixture)
    result = extract_form_fields(html, form_selector)
    assert result == expected
