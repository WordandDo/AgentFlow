"""
Agent Runner - executes agent on tasks using sandbox tools

Handles multi-turn conversation with tool calling.
"""

import json
import time
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional
import bdb

# Import sandbox components directly to avoid server module dependencies
from sandbox import Sandbox, format_tool_result

from .config import RolloutConfig
from .logging_utils import get_context, get_logger, set_context, clear_context


def _ctx_trace() -> str:
    """Convenience helper: pull the current trace_id from the log context."""
    return get_context().get("trace_id", "-")
from .models import (
    BenchmarkItem, Trajectory, Message, ToolCall, TaskResult
)
from .utils import (
    create_openai_client,
    async_chat_completion,
    extract_final_answer,
    convert_tool_schema_to_openai,
    format_tool_result_for_message,  # kept as a fallback only
)


log = get_logger("rollout.runner")


def _make_trace_id(run_id: str, worker_id: str, task_id: str, turn: int, suffix: str) -> str:
    """Build a stable, human-readable trace_id for a single tool call.

    Format: ``<run>:<worker>:<task>:t<turn>:<tool_call_id_or_uuid>``.
    Keeps the rollout / sandbox / tool logs greppable for a single hop.
    """
    return f"{run_id}:{worker_id}:{task_id}:t{turn}:{suffix or uuid.uuid4().hex[:8]}"


class AgentRunner:
    """
    Agent Runner that executes tasks using sandbox tools.
    
    Supports multi-turn conversation with tool calling through OpenAI API.
    """

    def __init__(self, config: RolloutConfig, worker_id: Optional[str] = None,
                 run_id: Optional[str] = None):
        """Initialize agent runner.

        ``run_id`` is the per-pipeline identity used to scope trace ids;
        defaults to a short uuid so isolated invocations still produce
        unique traces.
        """
        self.config = config
        self.worker_id = worker_id or f"runner_{int(time.time())}"
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"

        # Create OpenAI client
        self.client = create_openai_client(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
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
            
            return TaskResult(
                task_id=task.id,
                question=task.question,
                predicted_answer=final_answer,
                ground_truth=task.answer,
                trajectory=trajectory if self.config.save_trajectories else None,
                success=True,
                metadata=task.metadata
            )
            
        except Exception as e:
            if isinstance(e, bdb.BdbQuit):
                raise
            
            trajectory.success = False
            trajectory.error = str(e)
            trajectory.end_time = datetime.now().isoformat()
            trajectory.execution_time_ms = (time.time() - start_time) * 1000
            
            print(f"❌ Task {task.id} failed: {e}")
            
            return TaskResult(
                task_id=task.id,
                question=task.question,
                predicted_answer="",
                ground_truth=task.answer,
                trajectory=trajectory if self.config.save_trajectories else None,
                success=False,
                error=str(e),
                metadata=task.metadata
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
            response = await async_chat_completion(
                self.client,
                model=self.config.model_name,
                messages=api_messages,
                tools=self.tool_schemas if self.tool_schemas else None,
                max_retries=self.config.max_retries,
                llm_timeout=self.config.llm_timeout,
            )
            
            assistant_message = response.choices[0].message
            
            # Convert to our Message format
            msg = Message(
                role="assistant",
                content=assistant_message.content or "",
                tool_calls=[tc.model_dump() for tc in assistant_message.tool_calls] if assistant_message.tool_calls else None
            )
            messages.append(msg)
            trajectory.messages.append(msg)
            trajectory.total_turns = turn_count + 1
            
            # Check if there are tool calls
            if assistant_message.tool_calls:
                # Execute tool calls
                for tool_call in assistant_message.tool_calls[:1]:  # Execute one at a time
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
        except asyncio.TimeoutError:
            msg = f"tool_timeout_{int(timeout)}s"
            log.warning("tool timeout: %s after %.1fs (trace=%s)", tool_name, timeout, trace_id)
            return ({
                "code": -2,
                "message": msg,
                "data": None,
                "meta": {"trace_id": trace_id, "tool": tool_name},
            }, effective_parameters)
        except Exception as e:
            print(f"    ❌ Tool execution error: {e}")
            log.exception("tool execution failed: %s (trace=%s)", tool_name, trace_id)
            return ({
                "code": -1,
                "message": str(e),
                "data": None,
                "meta": {"trace_id": trace_id, "tool": tool_name},
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
        ``format_tool_result_for_message`` helper so trajectories never
        stall on an unknown tool type.
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
        return format_tool_result_for_message(tool_result)


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
