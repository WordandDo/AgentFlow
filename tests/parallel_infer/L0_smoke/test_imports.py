"""
L0 smoke: every new public symbol introduced by feat/parallel-infer-v2
must be importable. If any of these break, the rest of the test suite
becomes meaningless, so we fail fast here.
"""

from __future__ import annotations

import importlib

import pytest


# (module path, attribute name) tuples. We assert each attribute exists
# on the freshly-imported module.
EXPECTED_SYMBOLS = [
    # 0.1 logging utils
    ("rollout.core.logging_utils", "get_logger"),
    ("rollout.core.logging_utils", "install_root_handler"),
    ("rollout.core.logging_utils", "set_context"),
    ("rollout.core.logging_utils", "clear_context"),
    ("rollout.core.logging_utils", "attach_worker_file_handler"),
    ("rollout.core.logging_utils", "detach_handler"),
    ("rollout.core.logging_utils", "Progress"),
    # 0.2 shutdown
    ("rollout.core.shutdown", "ShutdownManager"),
    # 1.1 async client + 0.7d last-match
    ("rollout.core.utils", "create_async_openai_client"),
    ("rollout.core.utils", "async_chat_completion"),
    ("rollout.core.utils", "extract_final_answer"),
    ("rollout.core.utils", "format_tool_result_for_message"),
    # 0.3 / 3.x persistence
    ("rollout.core.result_store", "ResultStore"),
    ("rollout.core.result_store", "ResultStoreLockError"),
    ("rollout.core.checkpoint_store", "CheckpointStore"),
    # 0.4 / 2.4 runner internals
    ("rollout.core.runner", "AgentRunner"),
    ("rollout.core.runner", "_make_trace_id"),
    ("rollout.core.runner", "_classify_tool_error"),
    ("rollout.core.runner", "_compute_tool_stats"),
    ("rollout.core.runner", "_aggregate_tool_stats"),
    # 0.5 config
    ("rollout.core.config", "RolloutConfig"),
    # Models with new fields
    ("rollout.core.models", "ToolCall"),
    ("rollout.core.models", "TaskResult"),
    ("rollout.core.models", "RolloutSummary"),
    # Pipeline
    ("rollout.pipeline", "RolloutPipeline"),
    # Sandbox client/server
    ("sandbox.sandbox", "Sandbox"),
    ("sandbox.sandbox", "SandboxConfig"),
    ("sandbox.client", "HTTPServiceClient"),
    ("sandbox.protocol", "ExecuteRequest"),
    ("sandbox.server.core.backpressure", "Bound"),
    ("sandbox.server.core.backpressure", "LaneGroup"),
    ("sandbox.server.core.backpressure", "BackpressureManager"),
    ("sandbox.server.core.resource_router", "ResourceRouter"),
    ("sandbox.server.core.tool_executor", "ToolExecutor"),
]


@pytest.mark.parametrize("module_path,attr", EXPECTED_SYMBOLS)
def test_symbol_importable(module_path: str, attr: str) -> None:
    mod = importlib.import_module(module_path)
    assert hasattr(mod, attr), f"{module_path} is missing attribute `{attr}`"


def test_rollout_package_reexports():
    """`from rollout import get_logger, install_root_handler` should work
    (these are the two top-level entry points wired into `rollout/__init__.py`;
    the other helpers stay namespaced under `rollout.core.logging_utils`)."""
    import rollout

    for name in [
        "get_logger",
        "install_root_handler",
    ]:
        assert hasattr(rollout, name), f"rollout.__init__ does not re-export {name}"


def test_rollout_core_reexports():
    """`rollout.core` re-exports the new async helpers and CheckpointStore."""
    import rollout.core as core

    for name in [
        "create_async_openai_client",
        "create_openai_client",
        "extract_final_answer",
        "CheckpointStore",
        "RolloutConfig",
    ]:
        assert hasattr(core, name), f"rollout.core does not re-export {name}"
