"""
L1 / miscellaneous - small but important commits.

Covered:
- 0.7d: extract_final_answer picks the LAST matching answer, not the first.
- 0.9:  _derive_cleanup_interval clamps to [30, 300] and uses session_ttl//2.
"""

from __future__ import annotations

import pytest

from rollout.core.utils import extract_final_answer
from sandbox.server.app import _derive_cleanup_interval


# -----------------------------------------------------------------------
# Commit 0.7d - extract_final_answer (last-match wins)
# -----------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    # Model self-corrects: the LATER answer must win.
    ("The answer is 42. Wait, actually the answer is 43.", "43"),
    # Explicit **Answer**: marker beats earlier free-text claims.
    ("Maybe 41? Therefore 42.\n**Answer**: 43", "43"),
    # Numeric answer (3.14) must NOT be split on the dot - the period is
    # immediately followed by a digit, not whitespace, so the answer stays
    # intact.
    ("The answer is 3.14", "3.14"),
    # When no marker exists, fall back to last non-empty line.
    ("line A\n\nline B\n\nline C", "line C"),
])
def test_extract_final_answer_last_match_wins(text, expected):
    assert extract_final_answer(text) == expected


def test_extract_final_answer_empty():
    assert extract_final_answer("") == ""


# -----------------------------------------------------------------------
# Commit 0.9 - cleanup_interval derivation
# -----------------------------------------------------------------------


@pytest.mark.parametrize("ttl,expected", [
    # Below clamp floor -> 30.
    (10, 30),
    (60, 30),
    # In linear range -> ttl // 2.
    (200, 100),
    (300, 150),
    (400, 200),
    # Above clamp ceiling -> 300.
    (600, 300),
    (1800, 300),
    (7200, 300),
])
def test_derive_cleanup_interval_within_range(ttl, expected):
    assert _derive_cleanup_interval(ttl) == expected


def test_derive_cleanup_interval_bad_input_falls_back():
    # Non-numeric input -> safe default (300).
    assert _derive_cleanup_interval("not-a-number") == 300  # type: ignore[arg-type]
    assert _derive_cleanup_interval(None) == 300  # type: ignore[arg-type]
