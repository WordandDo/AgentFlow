"""
Shared fixtures for `tests/parallel_infer/*`.

Goals:
- Keep tests fully offline (no LLM key, no sandbox server) at L0/L1/L2.
- Provide a single, well-known path to add the repo root to sys.path so any
  test can do `from rollout.core import RolloutConfig` etc.
- Provide an isolated `tmp_path`-style fixture that scrubs env vars which
  could leak into the test (OPENAI_*, AGENTFLOW_*).
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """Make sure no host secrets leak into the test process."""
    for var in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG",
        "AGENTFLOW_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _quiet_logging(caplog):
    """Most tests should not flood stderr with INFO logs."""
    caplog.set_level(logging.WARNING)
    yield


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
