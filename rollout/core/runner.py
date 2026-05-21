"""
Agent Runner - executes agent on tasks using sandbox tools

Handles multi-turn conversation with tool calling.
"""

import json
import time
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import bdb

# Import sandbox components directly to avoid server module dependencies
from sandbox import Sandbox, format_tool_result

from .config import RolloutConfig
from .checkpoint_store import CheckpointStore
from .logging_utils import get_context, get_logger, set_context, clear_context
from .models import (
    BenchmarkItem, Trajectory, Message, ToolCall, TaskResult
)
from .utils import (
    create_async_openai_client,
    async_chat_completion,
    extract_final_answer,
    convert_tool_schema_to_openai,
    format_tool_result_for_message,  # kept as a fallback only
)


log = get_logger("rollout.runner")


def _ctx_trace() -> str:
    """Convenience helper: pull the current trace_id from the log context."""
    return get_context().get("trace_id", "-")


def _make_trace_id(run_id: str, worker_id: str, task_id: str, turn: int, suffix: str) -> str:
    """Build a stable, human-readable trace_id for a single tool call.

    Format: ``<run>:<worker>:<task>:t<turn>:<tool_call_id_or_uuid>``.
    Keeps the rollout / sandbox / tool logs greppable for a single hop.
    """
    return f"{run_id}:{worker_id}:{task_id}:t{turn}:{suffix or uuid.uuid4().hex[:8]}"


def _classify_tool_error(
    exc: BaseException,
) -> Tuple[int, str, str, Optional[int]]:
    """Map a tool-execution exception to ``(code, kind, message, status_code)``.

    Phase 0+ / commit 0.4c-b (ENG-23). The numeric codes use the
    `<0` convention to avoid colliding with server business codes,
    and match the buckets in the plan:

      -1   unknown / fallback
      -2   tool-level wait_for timeout (already produced upstream)
      -20  httpx ConnectError (DNS / TCP fail)
      -21  httpx TimeoutException (read/write timeout)
      -22  HTTPClientError 4xx
      -23  HTTPClientError 5xx
      -30  SandboxConnectionError (client never connected)

    `kind` is the stable label written to `ToolCall.error_kind`;
    aggregations and `jq` filters should prefer it over the numeric
    code (codes get appended to but kinds stay stable).
    """
    # Lazy imports keep the runner import-light when nobody calls a
    # tool (e.g. unit-test contexts).
    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover - httpx is a hard dep
        httpx = None  # type: ignore[assignment]
    try:
        from sandbox.client import HTTPClientError  # type: ignore
    except Exception:  # pragma: no cover
        HTTPClientError = None  # type: ignore[assignment]
    try:
        from sandbox.sandbox import SandboxConnectionError  # type: ignore
    except Exception:  # pragma: no cover
        SandboxConnectionError = None  # type: ignore[assignment]

    msg = str(exc)

    # Order matters: HTTPClientError carries the status_code we need to
    # split 4xx vs 5xx, but it inherits from Exception (not httpx.HTTPError),
    # so try the narrowest classes first.
    if HTTPClientError is not None and isinstance(exc, HTTPClientError):
        status = getattr(exc, "status_code", None)
        if status is not None and 400 <= status < 500:
            return (-22, "client_error", msg, int(status))
        if status is not None and 500 <= status < 600:
            return (-23, "server_error", msg, int(status))
        # Other HTTPClientError (e.g. raised from transport layer):
        # fall through to the more specific httpx classes below.

    if httpx is not None:
        # asyncio.TimeoutError is a subclass of httpx.TimeoutException
        # in modern httpx, but the runner's own task-level wait_for
        # path catches that BEFORE this helper sees it; here, anything
        # left is a network read/write timeout.
        if isinstance(exc, getattr(httpx, "ConnectError", ())):
            return (-20, "connect", msg, None)
        if isinstance(exc, getattr(httpx, "TimeoutException", ())):
            return (-21, "timeout", msg, None)
        if isinstance(exc, getattr(httpx, "HTTPError", ())):
            return (-23, "http", msg, None)

    if SandboxConnectionError is not None and isinstance(
        exc, SandboxConnectionError
    ):
        return (-30, "sandbox_disconnect", msg, None)

    if isinstance(exc, asyncio.TimeoutError):
        return (-21, "timeout", msg, None)
    if isinstance(exc, asyncio.CancelledError):
        return (-10, "cancelled", msg, None)

    return (-1, "unknown", msg, None)


def _classify_failure_stage(exc: BaseException) -> str:
    """Best-effort mapping from an exception type to a failure stage.

    The bucket names are picked to be greppable in `results_*.jsonl`:
    ``llm`` / ``tool`` / ``task`` / ``connect`` / ``guard``. The full
    per-status classification (4xx vs 5xx, etc.) is deferred to the
    larger 0.4c-b commit; this helper is intentionally a small switch
    so the resume commit stays focused.
    """
    name = type(exc).__name__
    if name in ("TimeoutError", "asyncio.TimeoutError"):
        return "timeout"
    # `openai` errors are imported lazily to keep import-light.
    try:
        import openai  # type: ignore
        if isinstance(exc, getattr(openai, "APIError", ())):
            return "llm"
    except Exception:  # pragma: no cover - openai always installed
        pass
    try:
        import httpx  # type: ignore
        if isinstance(exc, getattr(httpx, "ConnectError", ())):
            return "connect"
        if isinstance(exc, getattr(httpx, "HTTPError", ())):
            return "http"
    except Exception:  # pragma: no cover
        pass
    if "SandboxConnection" in name:
        return "sandbox_connect"
    if "HTTPClient" in name:
        return "http"
    return "task"


def _is_retryable_failure(exc: BaseException) -> bool:
    """Should resume mode retry this failure on its own?

    Conservative defaults: only flag obviously-transient failures
    (timeouts, network blips) as retryable. Anything else has to be
    explicitly retried by the operator via `resume_retry_failed=True`.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        import httpx  # type: ignore
        if isinstance(exc, getattr(httpx, "HTTPError", ())):
            return True
    except Exception:  # pragma: no cover
        pass
    return False


def _compute_tool_stats(trajectory: Trajectory) -> Optional[Dict[str, Any]]:
    """Aggregate per-trajectory tool-call counts.

    Returns ``None`` when the trajectory has no tool calls so the field
    is absent in `TaskResult.to_dict()` and downstream readers don't
    have to disambiguate "empty stats" from "tool stats not computed".

    The structure intentionally mirrors what the Phase 2 summary will
    aggregate over: top-line `total/success/failed/success_rate`,
    plus `by_tool` and `by_code` breakdowns.
    """
    tcs = trajectory.tool_calls or []
    total = len(tcs)
    if total == 0:
        return None

    success = sum(1 for tc in tcs if tc.success)
    by_tool: Dict[str, Dict[str, int]] = {}
    by_code: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    total_exec_ms = 0.0

    for tc in tcs:
        entry = by_tool.setdefault(
            tc.tool_name, {"total": 0, "success": 0, "failed": 0}
        )
        entry["total"] += 1
        if tc.success:
            entry["success"] += 1
        else:
            entry["failed"] += 1
        if tc.code is not None:
            key = str(tc.code)
            by_code[key] = by_code.get(key, 0) + 1
        if not tc.success and tc.error_kind:
            by_kind[tc.error_kind] = by_kind.get(tc.error_kind, 0) + 1
        try:
            total_exec_ms += float(tc.execution_time_ms or 0.0)
        except (TypeError, ValueError):
            pass

    out: Dict[str, Any] = {
        "total": total,
        "success": success,
        "failed": total - success,
        "success_rate": (success / total) if total else 0.0,
        "execution_time_ms_total": round(total_exec_ms, 3),
        "by_tool": by_tool,
        "by_code": by_code,
    }
    if by_kind:
        out["by_error_kind"] = by_kind
    return out


def _aggregate_tool_stats(results: List["TaskResult"]) -> Optional[Dict[str, Any]]:
    """Roll up per-task `tool_stats` into a single summary dict.

    Sums counts; the per-tool and per-code maps are merged additively.
    Returns ``None`` when no task carries `tool_stats` so the summary
    consumer can omit the field cleanly.
    """
    have_any = False
    total = success = 0
    total_ms = 0.0
    by_tool: Dict[str, Dict[str, int]] = {}
    by_code: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for r in results:
        st = r.tool_stats
        if not isinstance(st, dict):
            continue
        have_any = True
        total += int(st.get("total", 0) or 0)
        success += int(st.get("success", 0) or 0)
        try:
            total_ms += float(st.get("execution_time_ms_total", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        for name, sub in (st.get("by_tool") or {}).items():
            if not isinstance(sub, dict):
                continue
            entry = by_tool.setdefault(name, {"total": 0, "success": 0, "failed": 0})
            entry["total"] += int(sub.get("total", 0) or 0)
            entry["success"] += int(sub.get("success", 0) or 0)
            entry["failed"] += int(sub.get("failed", 0) or 0)
        for code, count in (st.get("by_code") or {}).items():
            try:
                by_code[str(code)] = by_code.get(str(code), 0) + int(count or 0)
            except (TypeError, ValueError):
                continue
        for kind, count in (st.get("by_error_kind") or {}).items():
            try:
                by_kind[str(kind)] = by_kind.get(str(kind), 0) + int(count or 0)
            except (TypeError, ValueError):
                continue

    if not have_any:
        return None

    out: Dict[str, Any] = {
        "total": total,
        "success": success,
        "failed": total - success,
        "success_rate": (success / total) if total else 0.0,
        "execution_time_ms_total": round(total_ms, 3),
        "by_tool": by_tool,
        "by_code": by_code,
    }
    if by_kind:
        out["by_error_kind"] = by_kind
    return out


class AgentRunner:
    """
    Agent Runner that executes tasks using sandbox tools.
    
    Supports multi-turn conversation with tool calling through OpenAI API.
    """

    def __init__(self, config: RolloutConfig, worker_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 checkpoint_store: Optional[CheckpointStore] = None):
        """Initialize agent runner.

        ``run_id`` is the per-pipeline identity used to scope trace ids;
        defaults to a short uuid so isolated invocations still produce
        unique traces. ``checkpoint_store`` (optional, Phase 3 / commit
        3.3) receives a per-turn `Trajectory.to_dict()` snapshot so a
        kill -9 mid-task leaves an auditable artifact on disk.
        """
        self.config = config
        self.worker_id = worker_id or f"runner_{int(time.time())}"
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        self.checkpoint_store: Optional[CheckpointStore] = checkpoint_store

        # Create the async OpenAI client. We build this in __init__
        # rather than start() so callers can construct the runner from
        # any context (sync or async) without changing the public API;
        # the underlying httpx.AsyncClient is lazily bound to whichever
        # event loop ultimately awaits the first request.
        self.client = create_async_openai_client(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_connections=self.config.llm_max_connections,
            max_keepalive=self.config.llm_max_keepalive,
            timeout_s=self.config.llm_timeout,
            connect_timeout_s=self.config.llm_connect_timeout,
        )

        # Sandbox instance (will be created in start())
        self.sandbox: Optional[Sandbox] = None
        
        # Tool schemas (OpenAI format)
        self.tool_schemas: List[Dict[str, Any]] = []
        
        # Local tool schemas for prompts
        self._local_tool_schemas: List[Dict[str, Any]] = []
        
        self._started = False

    async def start(self) -> bool:
        """Start the runner (initialize sandbox and load tools)"""
        if self._started:
            return True
        
        try:
            print(f"[Runner {self.worker_id}] Starting...")
            
            # Create sandbox instance
            self.sandbox = Sandbox(
                server_url=self.config.sandbox_server_url,
                worker_id=self.worker_id,
                auto_start_server=self.config.sandbox_auto_start,
                server_config_path=self.config.sandbox_config_path,
                timeout=self.config.sandbox_timeout,
                retry_count=self.config.sandbox_retry_max,
                retry_delay=self.config.sandbox_retry_backoff_base,
                retry_jitter=self.config.sandbox_retry_jitter,
                warmup_resources=self.config.resource_types if self.config.resource_types else None
            )
            
            # Start sandbox
            await self.sandbox.start()
            
            # Create sessions for required resources
            if self.config.resource_types:
                print(f"[Runner {self.worker_id}] Creating sessions for: {self.config.resource_types}")
                
                resource_configs = {}
                for resource_type in self.config.resource_types:
                    init_config = self.config.resource_init_configs.get(resource_type, {})
                    resource_configs[resource_type] = init_config.get("content", {}) if init_config else {}
                
                result = await self.sandbox.create_session(resource_configs)
                
                if result.get("status") not in ("success", "partial"):
                    print(f"[Runner {self.worker_id}] ⚠️ Session creation issue: {result}")
            
            # Load tool schemas
            await self._load_tool_schemas()
            
            self._started = True
            print(f"[Runner {self.worker_id}] ✅ Started successfully")
            print(f"[Runner {self.worker_id}] Available tools: {[t['function']['name'] for t in self.tool_schemas]}")
            return True
            
        except Exception as e:
            if isinstance(e, bdb.BdbQuit):
                raise
            print(f"[Runner {self.worker_id}] ❌ Failed to start: {e}")
            import traceback
            traceback.print_exc()
            await self.stop()
            return False

    async def stop(self) -> None:
        """Cancel-safe stop.

        Even when the outer task is being cancelled (Ctrl+C), best-effort
        finish: tell the sandbox server to destroy our worker's sessions
        and close the HTTP client so connections are not leaked. Each
        step is wrapped in its own ``asyncio.shield + wait_for`` so a
        misbehaving session cannot block forever and so the cancellation
        of the enclosing task does not interrupt cleanup mid-flight.
        """
        try:
            if self.sandbox:
                if self.config.resource_types:
                    try:
                        await asyncio.shield(
                            asyncio.wait_for(
                                self.sandbox.destroy_session(self.config.resource_types),
                                timeout=10.0,
                            )
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                        log.warning(
                            "destroy_session timed out/cancelled during stop "
                            "(worker=%s); server TTL will reclaim: %r",
                            self.worker_id, e,
                        )
                    except Exception as e:
                        log.warning(
                            "destroy_session failed during stop (worker=%s): %r",
                            self.worker_id, e,
                        )

                try:
                    await asyncio.shield(
                        asyncio.wait_for(self.sandbox.close(), timeout=5.0)
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    log.warning(
                        "sandbox.close() timed out/cancelled (worker=%s): %r",
                        self.worker_id, e,
                    )
                except Exception as e:
                    log.warning(
                        "sandbox.close() failed (worker=%s): %r", self.worker_id, e
                    )

                self.sandbox = None

            # Close the LLM client after sandbox cleanup so any in-flight
            # tool calls that the runner is no longer waiting on don't
            # race with httpx pool teardown.
            if self.client is not None:
                try:
                    await asyncio.shield(
                        asyncio.wait_for(self.client.close(), timeout=5.0)
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    log.warning(
                        "LLM client close timed out/cancelled (worker=%s): %r",
                        self.worker_id, e,
                    )
                except Exception as e:
                    log.warning(
                        "LLM client close failed (worker=%s): %r",
                        self.worker_id, e,
                    )
                finally:
                    self.client = None

        finally:
            self._started = False
            log.info("runner stopped (worker=%s)", self.worker_id)

    async def _load_tool_schemas(self) -> None:
        """Load tool schemas from sandbox or local definitions"""
        # Import local tool schemas
        from sandbox.tool_schemas import get_tool_schemas
        
        # Get schemas for allowed tools
        allowed_tools = self.config.available_tools if self.config.available_tools else None
        self._local_tool_schemas = get_tool_schemas(allowed_tools)
        
        # Convert to OpenAI format
        self.tool_schemas = [
            convert_tool_schema_to_openai(schema)
            for schema in self._local_tool_schemas
        ]

    async def run_task(self, task: BenchmarkItem) -> TaskResult:
        """Run agent on a single task with a hard task-level timeout.

        Wraps :meth:`_run_task_inner` in ``asyncio.wait_for`` using
        ``config.task_max_seconds``. On timeout we synthesise a failing
        ``TaskResult`` so the caller (pipeline) can still record the
        attempt and move on.
        """
        if not self._started:
            raise RuntimeError("Runner not started. Call start() first.")

        timeout = float(self.config.task_max_seconds)
        try:
            return await asyncio.wait_for(self._run_task_inner(task), timeout=timeout)
        except asyncio.TimeoutError:
            err = f"task_timeout_{int(timeout)}s"
            log.error("task timeout after %.1fs (task=%s)", timeout, task.id)
            print(f"⏰ Task {task.id} hit task_max_seconds={timeout:.0f}s")
            return TaskResult(
                task_id=task.id,
                question=task.question,
                predicted_answer="",
                ground_truth=task.answer,
                success=False,
                error=err,
                metadata=task.metadata,
                # Phase 3 / commit 3.2: structured failure classification.
                task_status="task_timeout",
                task_fail=True,
                failure_stage="task",
                failure_type="TimeoutError",
                failure_message=err,
                retryable=True,
            )

    async def _run_task_inner(self, task: BenchmarkItem) -> TaskResult:
        """Actual task body, called under the task-level wait_for above."""
        print(f"\n{'='*60}")
        print(f"Task: {task.id}")
        print(f"Question: {task.question[:200]}...")
        if task.kwargs:
            print(f"Kwargs: {task.kwargs}")
        print(f"{'='*60}")

        start_time = time.time()
        trajectory = Trajectory(
            task_id=task.id,
            question=task.question,
            start_time=datetime.now().isoformat()
        )

        try:
            # Build initial messages
            system_prompt = self.config.get_system_prompt()
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=task.question)
            ]
            trajectory.messages = messages.copy()
            
            # Run conversation loop with task kwargs
            final_answer = await self._run_conversation(messages, trajectory, task_kwargs=task.kwargs)
            
            trajectory.final_answer = final_answer
            trajectory.success = True
            trajectory.end_time = datetime.now().isoformat()
            trajectory.execution_time_ms = (time.time() - start_time) * 1000
            
            print(f"✅ Task {task.id} completed")
            print(f"   Final answer: {final_answer[:100]}...")
            
            tool_stats = _compute_tool_stats(trajectory)
            return TaskResult(
                task_id=task.id,
                question=task.question,
                predicted_answer=final_answer,
                ground_truth=task.answer,
                trajectory=trajectory if self.config.save_trajectories else None,
                success=True,
                metadata=task.metadata,
                tool_stats=tool_stats,
                task_status="completed",
                task_fail=False,
            )
            
        except Exception as e:
            if isinstance(e, bdb.BdbQuit):
                raise

            trajectory.success = False
            trajectory.error = str(e)
            trajectory.end_time = datetime.now().isoformat()
            trajectory.execution_time_ms = (time.time() - start_time) * 1000

            print(f"❌ Task {task.id} failed: {e}")

            tool_stats = _compute_tool_stats(trajectory)
            stage = _classify_failure_stage(e)
            # Best-effort breadcrumbs from the last recorded tool call:
            # tells operators which tool / trace_id the run died on.
            last_tool = (
                trajectory.tool_calls[-1].tool_name
                if trajectory.tool_calls else None
            )
            last_trace = (
                trajectory.tool_calls[-1].trace_id
                if trajectory.tool_calls else None
            )
            return TaskResult(
                task_id=task.id,
                question=task.question,
                predicted_answer="",
                ground_truth=task.answer,
                trajectory=trajectory if self.config.save_trajectories else None,
                success=False,
                error=str(e),
                metadata=task.metadata,
                tool_stats=tool_stats,
                task_status="failed",
                task_fail=True,
                failure_stage=stage,
                failure_type=type(e).__name__,
                failure_message=str(e)[:500],
                failed_turn=trajectory.total_turns or None,
                failed_tool_name=last_tool,
                failed_trace_id=last_trace,
                retryable=_is_retryable_failure(e),
            )

    async def _run_conversation(
        self,
        messages: List[Message],
        trajectory: Trajectory,
        task_kwargs: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Run multi-turn conversation until completion.
        
        Args:
            messages: Conversation messages
            trajectory: Trajectory to record
            task_kwargs: Additional kwargs to pass to tools (e.g., seed_path for doc tools)
        
        Returns final answer string.
        """
        if task_kwargs is None:
            task_kwargs = {}
        turn_count = 0
        
        while turn_count < self.config.max_turns:
            # Prepare messages for API
            api_messages = [m.to_dict() for m in messages]
            
            # Get response from LLM (with per-attempt llm_timeout).
            # Phase 0+ / commit 0.9: surface retry tuning from config.
            response = await async_chat_completion(
                self.client,
                model=self.config.model_name,
                messages=api_messages,
                tools=self.tool_schemas if self.tool_schemas else None,
                max_retries=self.config.max_retries,
                retry_wait=self.config.llm_retry_wait,
                retry_backoff=self.config.llm_retry_backoff,
                llm_timeout=self.config.llm_timeout,
            )
            
            assistant_message = response.choices[0].message

            # OpenAI tool protocol invariant: every `tool_call_id` recorded
            # on the assistant message must be answered by a matching
            # `role="tool"` message in the next turn, or the API rejects
            # the request. The runner currently executes only the first
            # tool call (`[:1]`), so the recorded `tool_calls` must be
            # truncated to that exact set, otherwise N>=2 silently
            # produces an unanswered tool_call_id and the next chat call
            # fails with "Invalid tool_call_id" or makes the model loop.
            assistant_tool_calls = list(assistant_message.tool_calls or [])
            executed_tool_calls = assistant_tool_calls[:1]

            if len(assistant_tool_calls) > 1:
                log.warning(
                    "model returned %d tool_calls; executing the first and "
                    "truncating recorded tool_calls to keep the assistant/tool "
                    "message pairing consistent (turn=%d)",
                    len(assistant_tool_calls), turn_count,
                )

            recorded_tool_calls = (
                [tc.model_dump() for tc in executed_tool_calls]
                if executed_tool_calls else None
            )

            msg = Message(
                role="assistant",
                content=assistant_message.content or "",
                tool_calls=recorded_tool_calls,
            )
            messages.append(msg)
            trajectory.messages.append(msg)
            trajectory.total_turns = turn_count + 1

            # Check if there are tool calls
            if executed_tool_calls:
                # Execute tool calls
                for tool_call in executed_tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    trace_id = _make_trace_id(
                        self.run_id, self.worker_id,
                        trajectory.task_id, turn_count, tool_call.id,
                    )
                    ctx_tokens = set_context(trace_id=trace_id)

                    print(f"  Turn {turn_count}: 🔧 {tool_name}")
                    print(f"    Args: {json.dumps(tool_args, ensure_ascii=False)[:200]}...")

                    try:
                        # Execute tool with task kwargs merged into parameters.
                        tool_result, effective_params = await self._execute_tool(
                            tool_name, tool_args, trace_id=trace_id, **task_kwargs)

                        # Structured fields from the canonical sandbox response.
                        is_dict = isinstance(tool_result, dict)
                        code = tool_result.get("code") if is_dict else None
                        message = tool_result.get("message", "") if is_dict else ""
                        meta = tool_result.get("meta") if is_dict else None
                        if not isinstance(meta, dict):
                            meta = {}
                        success = (code == 0) if code is not None else True

                        result_text = self._format_for_llm(tool_name, tool_result)

                        tc = ToolCall(
                            tool_name=tool_name,
                            parameters=tool_args,
                            result=tool_result.get("data") if is_dict else tool_result,
                            success=success,
                            error=None if success else (message or "tool failed"),
                            execution_time_ms=float(meta.get("execution_time_ms") or 0.0),
                            formatted_result=result_text,
                            code=code,
                            message=message,
                            resource_type=meta.get("resource_type"),
                            session_id=meta.get("session_id"),
                            trace_id=meta.get("trace_id") or trace_id,
                            effective_parameters=effective_params,
                            # Phase 0+ / commit 0.4c-b: surface error
                            # classification from _execute_tool's meta
                            # (and gracefully None for success rows).
                            error_kind=None if success else meta.get("error_kind"),
                            status_code=meta.get("status_code"),
                        )
                        trajectory.tool_calls.append(tc)

                        print(f"    Result: {result_text[:200]}...")

                        # Add tool result message
                        tool_msg = Message(
                            role="tool",
                            content=result_text,
                            tool_call_id=tool_call.id,
                            name=tool_name
                        )
                        messages.append(tool_msg)
                        trajectory.messages.append(tool_msg)
                    finally:
                        clear_context(ctx_tokens)

                # Phase 3 / commit 3.3 (optional): snapshot the
                # mid-task trajectory after every completed turn so a
                # kill -9 leaves an auditable artifact on disk. Write
                # off-loop (worker thread) so disk IO doesn't stall
                # the event loop at high concurrency. Failures are
                # swallowed inside `CheckpointStore.write_atomic`.
                if self.checkpoint_store is not None:
                    try:
                        await asyncio.to_thread(
                            self.checkpoint_store.write_atomic,
                            trajectory.task_id,
                            trajectory.to_dict(),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.debug("checkpoint write failed: %r", e)

                turn_count += 1
                continue
            
            else:
                # No tool calls, this is the final response
                final_answer = assistant_message.content or ""
                return extract_final_answer(final_answer)
        
        # Max turns reached
        print(f"⚠️ Max turns ({self.config.max_turns}) reached")
        
        # Try to extract answer from last assistant message
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.content:
                return extract_final_answer(msg.content)
        
        return "Max turns reached without answer"

    async def _execute_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        *,
        trace_id: Optional[str] = None,
        **kwargs,
    ) -> tuple:
        """
        Execute a tool via sandbox with a per-call timeout.
        
        Args:
            tool_name: Name of the tool to execute
            parameters: Tool parameters (from the LLM)
            trace_id: Optional trace id, forwarded so the rollout, client and
                server logs can be aligned on a single call.
            **kwargs: Additional kwargs to merge into parameters (e.g., seed_path for doc tools)
        
        Returns:
            ``(result, effective_parameters)`` where ``effective_parameters``
            is the merged kwargs+parameters dict that actually went to the
            sandbox. Always returned so callers can record both views even
            on failure.
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        
        # Merge kwargs into parameters (similar to synthesis worker).
        # This allows seed_path and other kwargs from benchmark jsonl to be
        # passed to tools. We preserve the *historical* precedence
        # (`{**parameters, **kwargs}` -> kwargs wins) so existing
        # benchmarks behave identically after the change. The original
        # LLM-provided dict is kept untouched in `ToolCall.parameters`
        # for audit; the merged dict is exposed as `effective_parameters`.
        effective_parameters = {**(parameters or {}), **(kwargs or {})}

        timeout = self._resolve_tool_timeout(tool_name)
        try:
            # Pass timeout to the sandbox both as an int hint for the server
            # *and* via asyncio.wait_for so a misbehaving server / network
            # stall cannot exceed the budget client-side.
            result = await asyncio.wait_for(
                self.sandbox.execute(
                    tool_name, effective_parameters,
                    trace_id=trace_id,
                    timeout=int(timeout),
                ),
                timeout=timeout,
            )
            return result, effective_parameters
        except bdb.BdbQuit:
            # Don't swallow pdb's quit signal; let the operator's exit
            # request bubble up to the top-level shutdown handler.
            raise
        except asyncio.CancelledError:
            # Cooperate with task-level cancellation; let it bubble.
            raise
        except asyncio.TimeoutError:
            # Tool-level wait_for hit. Use the canonical -2 / tool_timeout
            # bucket regardless of the inner exception so dashboards and
            # `error_kind` stay stable across all three timeout paths
            # (task / llm / tool).
            msg = f"tool_timeout_{int(timeout)}s"
            log.warning("tool timeout: %s after %.1fs (trace=%s)", tool_name, timeout, trace_id)
            return ({
                "code": -2,
                "message": msg,
                "data": None,
                "meta": {
                    "trace_id": trace_id,
                    "tool": tool_name,
                    "error_kind": "timeout",
                },
            }, effective_parameters)
        except Exception as e:
            # Phase 0+ / commit 0.4c-b: classify the failure so
            # `ToolCall.error_kind` and the response `meta` carry a
            # stable label (timeout / connect / client_error /
            # server_error / sandbox_disconnect / unknown). The numeric
            # code follows the negative-code convention so dashboards
            # can split rollout-side failures from server business codes.
            code, kind, message, status = _classify_tool_error(e)
            print(f"    ❌ Tool execution error [{kind}]: {e}")
            # Log at WARNING for known kinds (we already have a structured
            # error_kind to triage with), EXCEPTION for the unknown bucket
            # so a stack trace is captured exactly once per surprise.
            if kind == "unknown":
                log.exception(
                    "tool execution failed: %s (trace=%s, kind=unknown)",
                    tool_name, trace_id,
                )
            else:
                log.warning(
                    "tool execution failed: %s (trace=%s, kind=%s, status=%s): %s",
                    tool_name, trace_id, kind, status, message,
                )
            meta: Dict[str, Any] = {
                "trace_id": trace_id,
                "tool": tool_name,
                "error_kind": kind,
            }
            if status is not None:
                meta["status_code"] = status
            return ({
                "code": code,
                "message": message,
                "data": None,
                "meta": meta,
            }, effective_parameters)

    def _resolve_tool_timeout(self, tool_name: str) -> float:
        """Return the per-call timeout for ``tool_name``.

        Honours ``tool_timeout_overrides`` first, then falls back to
        ``tool_default_timeout``. Returned as float seconds.
        """
        overrides = self.config.tool_timeout_overrides or {}
        if tool_name in overrides:
            return float(overrides[tool_name])
        return float(self.config.tool_default_timeout)

    def _format_for_llm(self, tool_name: str, tool_result: Any) -> str:
        """Render a sandbox response into the string we send back to the LLM.

        Prefer the canonical ``sandbox.format_tool_result`` registry so
        the assistant sees the same human-friendly rendering everyone
        else does (no `code`/`meta`/`trace_id` noise leaking into the
        chat context, which both pollutes reasoning and wastes tokens).
        For tools without a registered formatter (custom/experimental)
        or for non-dict payloads, fall back to the generic
        ``format_tool_result_for_message`` helper (now length-bounded
        by ``config.tool_result_max_length``; Phase 0+ / commit 0.9)
        so trajectories never stall on an unknown tool type.
        """
        if isinstance(tool_result, dict):
            try:
                return format_tool_result(tool_result)
            except bdb.BdbQuit:
                raise
            except ValueError as e:
                log.warning(
                    "no formatter registered for tool %r (trace=%s): %s; "
                    "falling back to generic formatter",
                    tool_name, _ctx_trace(), e,
                )
            except Exception as e:
                log.exception(
                    "format_tool_result crashed for %r (trace=%s); "
                    "falling back to generic formatter: %r",
                    tool_name, _ctx_trace(), e,
                )
        return format_tool_result_for_message(
            tool_result, max_length=self.config.tool_result_max_length
        )


class SyncAgentRunner:
    """Synchronous wrapper for AgentRunner"""
    
    def __init__(self, config: RolloutConfig, worker_id: Optional[str] = None):
        self._runner = AgentRunner(config, worker_id)
    
    def _run_async(self, coro):
        """Run async coroutine in sync context"""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    
    def start(self) -> bool:
        return self._run_async(self._runner.start())
    
    def stop(self) -> None:
        self._run_async(self._runner.stop())
    
    def run_task(self, task: BenchmarkItem) -> TaskResult:
        return self._run_async(self._runner.run_task(task))
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
