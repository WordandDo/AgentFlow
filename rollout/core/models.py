"""
Data models for Rollout pipeline
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional
from datetime import datetime


@dataclass
class BenchmarkItem:
    """Single benchmark task item"""
    id: str
    question: str
    answer: Optional[str] = None  # Ground truth answer (if available)
    kwargs: Dict[str, Any] = field(default_factory=dict)  # Additional kwargs to pass to tools (e.g., seed_path for doc tools)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BenchmarkItem':
        """Create from dictionary"""
        # Extract kwargs if present (for doc tools, this contains seed_path)
        kwargs = data.get("kwargs", {})
        if not isinstance(kwargs, dict):
            kwargs = {}
        
        # Exclude standard fields and kwargs from metadata
        excluded_fields = {"id", "task_id", "question", "query", "input", "answer", "ground_truth", "expected", "kwargs"}
        metadata = {k: v for k, v in data.items() if k not in excluded_fields}
        
        return cls(
            id=str(data.get("id", data.get("task_id", ""))),
            question=data.get("question", data.get("query", data.get("input", ""))),
            answer=data.get("answer", data.get("ground_truth", data.get("expected", None))),
            kwargs=kwargs,
            metadata=metadata
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        result = {
            "id": self.id,
            "question": self.question,
        }
        if self.answer is not None:
            result["answer"] = self.answer
        if self.kwargs:
            result["kwargs"] = self.kwargs
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class ToolCall:
    """Single tool call record.

    Carries everything an operator needs to debug a single tool
    invocation. ``formatted_result`` is the human-readable string that
    was actually sent back to the LLM (i.e. the output of
    ``sandbox.format_tool_result``); ``result`` keeps the raw
    ``response["data"]`` for post-hoc analysis.

    ``parameters`` is what the LLM produced; ``effective_parameters``
    is what the runner actually sent to the sandbox after merging
    benchmark ``task_kwargs`` (e.g. ``seed_path``). Keeping both lets
    operators audit "why did the call use a different path than the
    one the model asked for?" without staring at the runner code.
    """
    tool_name: str
    parameters: Dict[str, Any]
    result: Any
    success: bool
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    # Structured fields lifted from the sandbox response. All optional so
    # older trajectories (without these) still load cleanly.
    formatted_result: str = ""
    code: Optional[int] = None
    message: str = ""
    resource_type: Optional[str] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    # Args after task_kwargs merge (i.e. what actually hit `sandbox.execute`).
    effective_parameters: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolCall':
        """Rebuild a ToolCall from its `to_dict` payload.

        Phase 3 / commit 0.8b: every field is read with ``.get(...,
        default)`` so older jsonl rows (missing later fields such as
        ``trace_id`` / ``effective_parameters``) deserialise cleanly.
        """
        return cls(
            tool_name=str(data.get("tool_name", "")),
            parameters=dict(data.get("parameters") or {}),
            result=data.get("result"),
            success=bool(data.get("success", True)),
            error=data.get("error"),
            execution_time_ms=float(data.get("execution_time_ms", 0.0) or 0.0),
            formatted_result=str(data.get("formatted_result", "") or ""),
            code=data.get("code"),
            message=str(data.get("message", "") or ""),
            resource_type=data.get("resource_type"),
            session_id=data.get("session_id"),
            trace_id=data.get("trace_id"),
            effective_parameters=data.get("effective_parameters"),
        )


@dataclass
class Message:
    """Single message in conversation"""
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # For tool messages

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (OpenAI compatible format)"""
        result = {"role": self.role, "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.name:
            result["name"] = self.name
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Message':
        """Create from dictionary"""
        return cls(
            role=data.get("role", ""),
            content=data.get("content", ""),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name")
        )


@dataclass
class Trajectory:
    """Complete conversation trajectory for a task"""
    task_id: str
    question: str
    messages: List[Message] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    final_answer: str = ""
    total_turns: int = 0
    success: bool = False
    error: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    execution_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "task_id": self.task_id,
            "question": self.question,
            "messages": [m.to_dict() for m in self.messages],
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "final_answer": self.final_answer,
            "total_turns": self.total_turns,
            "success": self.success,
            "error": self.error,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "execution_time_ms": self.execution_time_ms
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trajectory':
        """Create from dictionary.

        Phase 3 / commit 0.8b (ENG-32): now actually restores
        ``tool_calls`` via :meth:`ToolCall.from_dict` so resume,
        replay, and offline analysis can introspect them. Previously
        this returned an empty list, silently losing data on every
        round-trip through disk.
        """
        return cls(
            task_id=data.get("task_id", ""),
            question=data.get("question", ""),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            tool_calls=[ToolCall.from_dict(tc) for tc in (data.get("tool_calls") or [])],
            final_answer=data.get("final_answer", ""),
            total_turns=data.get("total_turns", 0),
            success=data.get("success", False),
            error=data.get("error"),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            execution_time_ms=data.get("execution_time_ms", 0.0)
        )


@dataclass
class TaskResult:
    """Result of a single task execution.

    ``tool_stats`` (added in Phase 2 / commit 2.4) is a small summary
    of how the trajectory's tool calls performed (counts by
    success/failure, per-tool, per-code). It is computed once by the
    runner so downstream readers don't have to walk the full
    trajectory. None for tasks that recorded no tool calls (or for
    very old trajectories before the field existed).

    Phase 3 / commit 3.2 (ENG-12): the failure-classification block
    (``task_status`` ... ``retryable``) is populated by
    ``AgentRunner`` on every failure path. Downstream consumers can
    then ``jq 'select(.task_fail==true)'`` to triage a run without
    grepping stack traces. All fields default to None so older
    trajectories deserialise cleanly.
    """
    task_id: str
    question: str
    predicted_answer: str
    ground_truth: Optional[str] = None
    trajectory: Optional[Trajectory] = None
    success: bool = False
    error: Optional[str] = None
    score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_stats: Optional[Dict[str, Any]] = None

    # Failure classification (Phase 3 / commit 3.2). Present on
    # failures only; success rows leave them None.
    task_status: Optional[str] = None  # "completed" | "failed" | "task_timeout"
    task_fail: Optional[bool] = None
    failure_stage: Optional[str] = None  # e.g. "llm", "tool", "task", "guard"
    failure_type: Optional[str] = None   # exception class name (TimeoutError, ...)
    failure_message: Optional[str] = None
    failed_turn: Optional[int] = None
    failed_tool_name: Optional[str] = None
    failed_trace_id: Optional[str] = None
    retryable: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        result = {
            "task_id": self.task_id,
            "question": self.question,
            "predicted_answer": self.predicted_answer,
            "success": self.success,
        }
        if self.ground_truth is not None:
            result["ground_truth"] = self.ground_truth
        if self.trajectory:
            result["trajectory"] = self.trajectory.to_dict()
        if self.error:
            result["error"] = self.error
        if self.score is not None:
            result["score"] = self.score
        if self.metadata:
            result["metadata"] = self.metadata
        if self.tool_stats is not None:
            result["tool_stats"] = self.tool_stats

        # Failure classification - only persist when populated so
        # success rows stay compact.
        for fld in (
            "task_status",
            "task_fail",
            "failure_stage",
            "failure_type",
            "failure_message",
            "failed_turn",
            "failed_tool_name",
            "failed_trace_id",
            "retryable",
        ):
            v = getattr(self, fld)
            if v is not None:
                result[fld] = v

        return result


@dataclass
class EvaluationResult:
    """Evaluation result for a single task"""
    task_id: str
    predicted: str
    ground_truth: str
    score: float
    metric: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


@dataclass
class RolloutSummary:
    """Summary of a complete rollout run.

    `tool_stats` (added in Phase 2 / commit 2.4) is the run-wide
    aggregate of per-task `TaskResult.tool_stats`; deliberately
    separate from the answer-correctness `average_score` so the two
    can be read independently.
    """
    benchmark_name: str
    total_tasks: int
    successful_tasks: int
    failed_tasks: int
    average_score: float
    metric: str
    total_time_seconds: float
    results_file: str
    evaluation_file: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    tool_stats: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)
