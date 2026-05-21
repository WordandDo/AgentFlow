"""
L0 smoke: every new field in `RolloutConfig` introduced by Phase 0/2/3
must be:
  (a) reachable as an attribute on the default object,
  (b) round-trippable through `from_dict / to_dict`,
  (c) covered by `validate()` (i.e. obviously bad values are caught).

The intent is to catch the most common "config drift" regressions
(removed field, renamed field, missing validator, missing key in
`to_dict`) before any heavy test runs.
"""

from __future__ import annotations

import pytest

from rollout.core.config import RolloutConfig


PHASE0_FIELDS = [
    "task_max_seconds",
    "llm_timeout",
    "tool_default_timeout",
    "tool_timeout_overrides",
    "tool_result_max_length",
]

PHASE2_FIELDS = [
    "concurrency",
    "worker_startup_jitter",
    "worker_startup_batch_size",
    "keep_results_in_memory",
    "on_duplicate_task_id",
    "output_filename_strategy",
]

PHASE3_FIELDS = [
    "resume",
    "resume_file",
    "resume_retry_failed",
    "checkpoint_enabled",
    "checkpoint_dir",
]

PHASE2S_FIELDS = [
    "llm_max_connections",
    "llm_max_keepalive",
    "sandbox_retry_max",
]

ALL_NEW_FIELDS = PHASE0_FIELDS + PHASE2_FIELDS + PHASE3_FIELDS + PHASE2S_FIELDS


@pytest.mark.parametrize("field", ALL_NEW_FIELDS)
def test_default_object_has_field(field: str) -> None:
    cfg = RolloutConfig()
    assert hasattr(cfg, field), f"RolloutConfig() is missing attribute `{field}`"


@pytest.mark.parametrize("field", ALL_NEW_FIELDS)
def test_to_dict_round_trip(field: str) -> None:
    cfg = RolloutConfig()
    payload = cfg.to_dict()
    assert field in payload, (
        f"RolloutConfig.to_dict() does not include `{field}` "
        f"(round-trip-safe configs require it)"
    )


def test_from_dict_accepts_known_keys():
    # We do not validate values here; we only want to confirm that
    # `from_dict` accepts every new key without crashing.
    payload = {f: RolloutConfig().to_dict()[f] for f in ALL_NEW_FIELDS}
    payload["model_name"] = "stub-model"
    cfg = RolloutConfig.from_dict(payload)
    for f in ALL_NEW_FIELDS:
        assert getattr(cfg, f) == payload[f]


def test_from_dict_legacy_max_workers_maps_to_concurrency():
    """Backward-compat: legacy `max_workers=8` configs should map onto
    `concurrency=8` and *not* be silently dropped (commit 2.1)."""
    cfg = RolloutConfig.from_dict({"max_workers": 8})
    assert cfg.concurrency == 8


def test_from_dict_explicit_concurrency_wins_over_legacy():
    cfg = RolloutConfig.from_dict({"max_workers": 8, "concurrency": 3})
    assert cfg.concurrency == 3


def test_validate_catches_bad_concurrency():
    cfg = RolloutConfig.from_dict({"concurrency": 0})
    errors = cfg.validate()
    # Either max_workers (legacy) or concurrency (new) should complain.
    # We accept either error message; the important thing is `validate`
    # actually returns a non-empty error list.
    assert errors, "validate() must reject concurrency=0 / max_workers=0"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("task_max_seconds", 0),
        ("llm_timeout", -1),
        ("tool_default_timeout", 0),
        ("tool_result_max_length", 64),  # below the 256 floor
    ],
)
def test_validate_rejects_unreasonable_timeouts(field: str, bad_value) -> None:
    cfg = RolloutConfig()
    setattr(cfg, field, bad_value)
    errors = cfg.validate()
    assert errors, f"validate() must reject {field}={bad_value}"


def test_validate_rejects_unknown_duplicate_mode():
    cfg = RolloutConfig()
    cfg.on_duplicate_task_id = "explode_pls"
    errors = cfg.validate()
    assert any("on_duplicate_task_id" in e for e in errors)


def test_validate_rejects_unknown_filename_strategy():
    cfg = RolloutConfig()
    cfg.output_filename_strategy = "magic"
    errors = cfg.validate()
    assert any("output_filename_strategy" in e for e in errors)


def test_explicit_filename_strategy_requires_filename():
    cfg = RolloutConfig()
    cfg.output_filename_strategy = "explicit"
    cfg.output_filename = ""
    errors = cfg.validate()
    assert any("explicit" in e for e in errors), (
        "explicit strategy without `output_filename` must be rejected"
    )
