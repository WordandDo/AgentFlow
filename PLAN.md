# [当前权威 · v2.7] AgentFlow rollout / sandbox 并发改造实施计划（commit 级）

> ✅ **本文件是当前唯一权威 plan（v2.7，2026-05-19）**，后续所有 commit 均按本文实施。
>
> 已废弃的历史文档（仅作设计演进档案，不维护、不实施）：
>
> - `PARALLEL_INFER_PLAN.md` — v1.0 设计稿（纯协程方案，被 GUI/VM session 污染问题推翻）
> - `plan.md` — v1.1 设计稿（worker-pool + 服务端解耦，工程细节未明示）
> - `IMPLEMENTATION_PLAN.md` — v2.2 快照（被本文 v2.3-v2.7 系列迭代取代）


---

# AgentFlow rollout / sandbox 并发改造实施计划（commit 级）

> 本文档把前两份设计稿（`PARALLEL_INFER_PLAN.md` v1.0、`plan.md` v1.1）落地成**精细到 commit 级别**的执行清单。每个 commit 单独可合并、单独可回滚、单独有验证测试。
>
> 目标：rollout 单进程协程支持 ~100 并发，worker-pool 模型保证有状态资源隔离，sandbox server 防风暴加固，Ctrl+C 优雅退出，断点续推可用。

---

## 元数据 (Metadata)


| 项          | 值                                                                       |
| ---------- | ----------------------------------------------------------------------- |
| 文档版本       | v2.7（implementation + heartbeat lease fallback execution guidance）             |
| 创建日期       | 2026-05-18                                                              |
| 状态         | Draft（已确认覆盖 Web/RAG + GUI/VM/Browser；执行口径以 §0 + §12.0.1 为准）              |
| 关联文档       | `PARALLEL_INFER_PLAN.md`（v1.0 设计）, `plan.md`（v1.1 设计）                   |
| 关联模块       | `rollout/`、`sandbox/`、`configs/infer/`                                  |
| 改造方式       | rollout 单进程 worker-pool + sandbox server 单进程防风暴；只走老 HTTP/tool schema 契约 |
| 目标并发量      | Web/RAG ≥ 100；GUI/VM/Browser 由物理资源决定（典型 8–16）                           |
| 向后兼容       | 主线默认兼容；个别审计补丁会改变可见行为，必须在对应 commit 的风险点里显式说明                              |
| 总代码量估算     | 主线 ~1500–1800 行；审计补丁与配置暴露另计 ~700–900 行（不含文档/sample/测试）             |
| 总计划条目       | 主线 22 个 + 审计补丁 14 个 + 配置暴露 1 个；实际执行优先级见 §0 与 §12.0.1               |


---

## 0. 总览：所有 commit 一览表

按依赖排序，前面的 commit **不依赖**后面的 commit。表格里**优先级/时机**列表示 v2.2 二次评审后的执行口径：主线仍用 ✓/△/○，审计补丁用 P0/P1/P2/P3/deferred。


| Phase | Commit | 主题                                                                              | 优先级/时机                                             | 估算 LoC |
| ----- | ------ | ------------------------------------------------------------------------------- | --------------------------------------------------- | ------ |
| 0     | 0.1    | `feat(rollout): add structured logger with contextvars + Progress`              | ✓                                                   | ~120   |
| 0     | 0.2    | `feat(rollout): add ShutdownManager with signal handling`                       | ✓                                                   | ~80    |
| 0     | 0.3    | `feat(rollout): cancel-safe pipeline shutdown + atomic result append`           | ✓                                                   | ~90    |
| 0     | 0.4    | `feat(rollout): propagate trace_id and capture structured ToolCall fields`      | ✓                                                   | ~110   |
| 0     | 0.5    | `feat(rollout): three-tier timeout (task / llm / tool)`                         | ✓                                                   | ~70    |
| 1     | 1.1    | `feat(rollout): switch LLM client to AsyncOpenAI with httpx limits`             | ✓                                                   | ~80    |
| 1     | 1.2    | `refactor(rollout): keep sync chat_completion for evaluator path`               | ✓                                                   | ~30    |
| 2     | 2.1    | `feat(rollout): add worker-pool config fields and validation`                   | ✓                                                   | ~80    |
| 2     | 2.2    | `feat(rollout): implement worker-pool scheduler in _run_parallel`               | ✓                                                   | ~180   |
| 2     | 2.3    | `feat(rollout): unique worker_id, per-worker logger, startup jitter`            | ✓                                                   | ~60    |
| 2     | 2.4    | `feat(rollout): aggregate tool execution stats per task and summary`            | ✓                                                   | ~80    |
| 2S    | 2S.1   | `fix(sandbox): split ResourceRouter lock; init outside global lock`             | ✓                                                   | ~150   |
| 2S    | 2S.2   | `feat(sandbox): tiered backpressure (health/status/session/tool)`               | ✓                                                   | ~120   |
| 2S    | 2S.3   | `feat(sandbox): per-(worker_id, resource_type) tool serial lock`                | ✓                                                   | ~60    |
| 2S    | 2S.4   | `fix(sandbox): shared websearch thread pool + httpx limits + heartbeat jitter`  | ✓                                                   | ~80    |
| 2S    | 2S.5   | `feat(sandbox): worker_disconnect on shutdown + server SIGTERM cleanup`         | ✓                                                   | ~80    |
| 3     | 3.1    | `feat(rollout): output filename strategy + file lock`                           | ✓                                                   | ~90    |
| 3     | 3.2    | `feat(rollout): resume by task_id with failure classification`                  | ✓                                                   | ~100   |
| 3     | 3.3    | `feat(rollout): checkpoint mid-task trajectories (optional)`                    | ○                                                   | ~140   |
| 4     | 4.1    | `docs(infer): add parallel sample configs and tuning guide`                     | ✓                                                   | ~150   |
| 5     | 5.1    | `feat(eval): EvaluationContext + offline/online_env modes`                      | ○                                                   | ~120   |
| 5     | 5.2    | `feat(eval): evaluate_async + evaluation cache for resume`                      | ○                                                   | ~120   |
| 0+    | 0.4a   | `fix(rollout): use sandbox.format_tool_result instead of inline json.dumps`     | P1                                                  | ~20    |
| 0+    | 0.4b   | `feat(sandbox): heartbeat refresh TTL as session lease fallback`                | P1（随 2S.4）                                          | ~70    |
| 0+    | 0.4c   | `fix(rollout): re-raise BdbQuit now; defer full tool error classification`      | P0 + P2                                             | ~60    |
| 0+    | 0.4d   | `feat(rollout): write back evaluator score to TaskResult`                       | P1                                                  | ~25    |
| 0+    | 0.4e   | `feat(rollout): record effective_parameters in ToolCall (post-merge args)`      | P2（或随 0.4 主体）                                      | ~35    |
| 0+    | 0.4f   | `feat(rollout): warn/abort on duplicate task_id in benchmark data`              | P1                                                  | ~20    |
| 2S    | 0.7a   | `fix(sandbox): destroy sessions on Sandbox.close by default`                    | ✓（随 2S.5）                                           | ~25    |
| 2S    | 0.7b   | `feat(sandbox): exponential backoff with jitter + 4xx no-retry`                 | ✓（随 2S/并发改造）                                       | ~60    |
| 0     | 0.7c   | `fix(rollout): atomic _save_result with flush+fsync`                            | 合并到 0.3                                             | ~30    |
| 0+    | 0.7d   | `fix(rollout): use last-match in extract_final_answer`                          | P3（数据验证后）                                          | ~20    |
| 2-    | 0.8a   | `fix(rollout): keep assistant tool_calls paired with executed tool messages`    | P1                                                  | ~60    |
| 3     | 0.8b   | `feat(rollout): ToolCall.from_dict + Trajectory.from_dict full restore`         | P2（随 3.2）                                           | ~50    |
| 0+    | 0.8c   | `refactor(rollout/sandbox): reuse single event loop in sync wrappers`           | P3 / 可删除                                            | ~80    |
| 5     | 0.8d   | `refactor(eval): structured-output + json mode for llm_judgement`               | P2（随 Phase 5）                                       | ~100   |
| 0+    | 0.9    | `feat(config): rationalize runtime magic numbers and derived cleanup interval`   | P2                                                  | ~150   |


图例：✓ = 本轮必做；○ = 可选增强；P0/P1/P2/P3/deferred = §12.0.1 二次评审后的审计补丁优先级。v2.3 已确认覆盖 GUI/VM/Browser，因此原本“跑 GUI/VM/Browser 时必做”的 Phase 2S 已升为本轮必做。`0+` 表示 Phase 0 之后、Phase 1 之前；`2-` 表示 Phase 2 之前必修；`3` 表示并到 Phase 3；`5` 表示并到 Phase 5。Commit 0.4a~0.8d 是 §12 审计补丁系列，Commit 0.9 来自 §13。

**建议合并顺序**：Phase 0（含 0.7c）→ §12 P0/P1 小补丁（0.4a/0.4c-BdbQuit/0.4d/0.4f，0.4e 视 0.4 主体决定）→ Phase 1 → 0.8a → Phase 2 → smoke test → Phase 2S（含 0.4b/0.7a/0.7b）→ GUI/VM/Browser smoke test → Phase 3 → Phase 4 → 可选 Phase 5。

---

## 1. 设计原则（同 v1.1，落地时不变）

1. **向后兼容**：旧配置零修改 → 旧行为完全一致。
2. **每 commit 独立**：可单独合并、单独压测、单独回滚。
3. **接口先于实现**：每处暴露稳定接口（`RolloutScheduler`、`ResultStore`、`CheckpointStore`、`EvaluationStore`），未来换实现不破坏调用方。
4. **限流分层**：worker 级 → 工具级 TokenBucket → httpx 连接池 → server 入口分层 Semaphore；每层职责清晰不互相覆盖。
5. **可观测优先于性能**：先把日志、trace_id、进度、stats 打扎实，再压并发。
6. **有状态资源独占 session**：GUI/VM/Browser/Bash/Code/MCP 一个并发 worker 独占一个 `worker_id` 下的 session。
7. **宁可快速背压不要无界排队**：高并发风暴下 server `429/503 + Retry-After`，client 指数退避 + jitter。
8. **Cancel 是一等公民**：所有 await 路径都要考虑 `asyncio.CancelledError`，cleanup 必须 cancel-safe。

---

## 2. 已识别的工程坑（每个 commit 都会显式映射到这里）


| 编号     | 工程坑                                                                                                                                                                    | 解决 commit                                            |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| ENG-1  | 当前 `async_chat_completion` 用 sync `openai.OpenAI` + `run_in_executor(None, ...)`，被默认线程池封顶 ~32 路                                                                        | 1.1                                                  |
| ENG-2  | `_run_parallel` 是空壳，`parallel=true` 等价于串行                                                                                                                              | 2.2                                                  |
| ENG-3  | 共享 `worker_id="main_runner"` → GUI/VM/Browser session 被多 task 污染                                                                                                       | 2.2 + 2.3                                            |
| ENG-4  | `ResourceRouter._lock` 在重 `init` 期间持有，N 个 worker 同时启动 → 串行化 N×30s                                                                                                      | 2S.1                                                 |
| ENG-5  | `websearch.py` 每次调用 `new ThreadPoolExecutor(5)`，100 worker × 5 = 500 线程瞬时                                                                                              | 2S.4                                                 |
| ENG-6  | 100 worker 齐步 30s 心跳 → 周期性尖峰                                                                                                                                           | 2S.4                                                 |
| ENG-7  | `pipeline._save_result` 同步 open + append，多协程并发可能交错                                                                                                                     | 0.3                                                  |
| ENG-8  | `print(...)` 满天飞，100 并发日志无 worker/task 上下文                                                                                                                             | 0.1                                                  |
| ENG-9  | 没有 SIGINT/SIGTERM handler，Ctrl+C 第二次直接 SIGKILL，session 残留                                                                                                              | 0.2 + 0.3 + 2S.5                                     |
| ENG-10 | 没有 task/LLM/tool 超时，单题卡死拖垮整个 worker                                                                                                                                    | 0.5                                                  |
| ENG-11 | results 文件每次新 timestamp，跑挂了无法续                                                                                                                                         | 3.1 + 3.2                                            |
| ENG-12 | task `success=False` 没有失败原因分类，排查靠 grep traceback                                                                                                                       | 3.2                                                  |
| ENG-13 | `ToolCall` 只存 `result/success/error`，丢失 `code/message/session_id/trace_id/execution_time_ms`                                                                           | 0.4                                                  |
| ENG-14 | 100 worker 同时 start → server 同时调 `create_session` → 启动风暴                                                                                                               | 2.3（jitter）+ 2S.1（拆锁）+ 2S.2（session_create_inflight） |
| ENG-15 | 大响应（screenshot/accessibility tree）100 worker 同时返回 → 内存峰值                                                                                                               | 2S.2（响应裁剪）                                           |
| ENG-16 | rollout 进程 SIGKILL 后 server 端 session 等 TTL（5min）才清                                                                                                                    | 2S.5（worker_disconnect 主动通知）                         |
| ENG-17 | GUI/VM/terminal 评测依赖 session 状态，rollout 完成后 session 已销毁                                                                                                                | 5.1（online_env evaluator）                            |
| ENG-18 | rollout 长 trajectory 跑到一半挂掉，半成品轨迹丢失                                                                                                                                    | 3.3（checkpoint）                                      |
| ENG-19 | Evaluator 同步串行，1000 条 LLM judge 拖慢迭代                                                                                                                                   | 5.2                                                  |
| ENG-20 | 双进程并发写同一份 stable 文件                                                                                                                                                    | 3.1（fcntl flock）                                     |
| ENG-21 | `runner.py` 已 `from sandbox import format_tool_result` 却在 line 294 用劣化版 `utils.format_tool_result_for_message`，把 `code/message/meta/trace_id` 全量 dump 给 LLM            | 0.4a                                                 |
| ENG-22 | 服务端 heartbeat endpoint (`routes.py:161`) 只 `list_worker_sessions` 而**不调用 `refresh_session`**，是 readonly 探针；真正延 TTL 的是 tool call（`tool_executor.py:314`）。配合默认 `session_ttl=300s` + 每 300s 一次的 `cleanup_expired`，**两次 tool call 间隔 >300s（LLM 长思考）或单次 tool call 自身 >300s（long bash/爬虫）会被服务端误杀**。注意：client 端 `auto_heartbeat=False` 本身不是 bug，开/关效果等价 | 0.4b（heartbeat 续租）+ §13.4（调大 `session_ttl` 兜底） |
| ENG-23 | `_execute_tool` 把 `httpx.TimeoutException / HTTPClientError / ConnectionError` 全部压扁成 `{"code": -1}`，`ToolCall` 无法区分 root cause；同时 `except Exception` 不排除 `bdb.BdbQuit` | 0.4c-a 立即修 BdbQuit；0.4c-b 并入 2.4 |
| ENG-24 | `TaskResult.score` 字段已定义但 `evaluator.evaluate` 不回填，jsonl 里 score 永远 None，必须 join 两个文件                                                                                  | 0.4d                                                 |
| ENG-25 | `ToolCall.parameters` 只记 LLM 原始 args，不记 `_execute_tool` 内部 `{**parameters, **kwargs}` 合并后真正发到 sandbox 的 effective_parameters                                           | 0.4e                                                 |
| ENG-26 | `load_benchmark_data` 不校验 `task_id` 唯一性，重复 id silent overwrite                                                                                                         | 0.4f                                                 |
| ENG-27 | `Sandbox.close()` 不调用 `client.close(destroy_sessions=True)`，`async with Sandbox()` 退出后 server 端 session 仍然 hang 着，靠 TTL 才清                                             | 0.7a                                                 |
| ENG-28 | `HTTPClientConfig` retry 是 `retry_delay * (attempt+1)` 线性、无 jitter、无 4xx/5xx 区分，100 并发遇 5xx 同步重试形成 thundering herd                                                     | 0.7b                                                 |
| ENG-29 | `pipeline._save_result` 非原子 append，无 `flush+fsync`，并发模式 + Ctrl+C 可能写半行 jsonl                                                                                           | 0.7c（与 0.3 收尾合并）                                     |
| ENG-30 | `extract_final_answer` 用第一个正则匹配，模型 "the answer is X… wait actually Y" 会取到 X                                                                                            | 0.7d                                                 |
| ENG-31 | `_run_conversation` 第 271 行硬编码 `assistant_message.tool_calls[:1]`，但 message 里 dump 了全部 N 个 tool_calls，下一轮喂回 LLM 时 tool_call_id/tool message 不配对，违反 OpenAI 协议           | 0.8a（Phase 2 前置；先截断记录，不执行全部 tool_calls）          |
| ENG-32 | `Trajectory.from_dict` 主动 `tool_calls=[]`，写出去的 jsonl 完整但反序列化丢弃，影响 resume / 重评 / 离线审计                                                                                   | 0.8b（Phase 3 前置）                                     |
| ENG-33 | `SyncAgentRunner._run_async` / `Sandbox._run_async` 每次都 `asyncio.new_event_loop()`，httpx.AsyncClient 绑定 loop，跨调用复用必报 `Event loop is closed`                            | 0.8c                                                 |
| ENG-34 | `evaluator._evaluate_llm_judgement` 评分解析逻辑非常脆弱（`"1" in content and "0" not in content.split("1")[0]` 误判 `10/10`，且 `except:` 裸 except 吞 `KeyboardInterrupt`）            | 0.8d（Phase 5 合并）                                     |


> **说明**：ENG-21～34 是 2026-05-18 与 Commit 0.4 同时做的全仓库审计发现的同性质问题，详细 commit 见 §12。

---

# Phase 0：基础设施加固

**目标**：在没引入任何并发改动之前，先把"100 并发跑炸了能否定位问题、能否安全退出"这两件事做完。**全部为零行为变化**（除日志格式、Ctrl+C 行为可见外）。

**预期收益**：

- Ctrl+C 一次发优雅退出信号，二次警告，三次强退（不再直接 SIGKILL）。
- 单题超时不再拖垮整体（即便仍是串行）。
- jsonl 写入原子可恢复（kill -9 后已写入的行都可解析）。
- 日志能按 `run_id / worker_id / task_id / trace_id` grep。
- 每次 tool call 在 rollout 端和 server 端有同一个 `trace_id`，三处日志可对齐。

---

## Commit 0.1: `feat(rollout): add structured logger with contextvars + Progress`

### 动机

ENG-8。把 `print(...)` 替换成结构化 logger。引入 `contextvars` 让 `run_id / worker_id / task_id / trace_id` 在协程切换间自动跟随，不需要在每个函数签名里手动透传。

### 修改文件


| 文件                              | 类型  | 改动                                                                           |
| ------------------------------- | --- | ---------------------------------------------------------------------------- |
| `rollout/core/logging_utils.py` | 新增  | `get_logger / set_context / clear_context / Progress / install_root_handler` |
| `rollout/__init__.py`           | 修改  | 导出 `get_logger`（可选）                                                          |


### 关键代码

```python
# rollout/core/logging_utils.py
import asyncio
import logging
import os
import sys
from contextvars import ContextVar
from typing import Optional, Dict, Any

_ctx_run_id: ContextVar[str] = ContextVar("run_id", default="-")
_ctx_worker_id: ContextVar[str] = ContextVar("worker_id", default="-")
_ctx_task_id: ContextVar[str] = ContextVar("task_id", default="-")
_ctx_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _ctx_run_id.get()
        record.worker_id = _ctx_worker_id.get()
        record.task_id = _ctx_task_id.get()
        record.trace_id = _ctx_trace_id.get()
        return True


def install_root_handler(level: str = "INFO") -> None:
    """Install once at pipeline entry; safe to call multiple times."""
    root = logging.getLogger()
    if getattr(root, "_agentflow_installed", False):
        return
    h = logging.StreamHandler(sys.stderr)
    fmt = ("%(asctime)s [%(levelname)s] %(name)s "
           "run=%(run_id)s w=%(worker_id)s task=%(task_id)s trace=%(trace_id)s | %(message)s")
    h.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    h.addFilter(_ContextFilter())
    root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root._agentflow_installed = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_context(**kw: Any) -> Dict[str, Any]:
    """Set context fields; returns a token dict for clear_context()."""
    tokens: Dict[str, Any] = {}
    for k, v in kw.items():
        if v is None:
            continue
        var = {"run_id": _ctx_run_id, "worker_id": _ctx_worker_id,
               "task_id": _ctx_task_id, "trace_id": _ctx_trace_id}.get(k)
        if var is not None:
            tokens[k] = var.set(str(v))
    return tokens


def clear_context(tokens: Dict[str, Any]) -> None:
    for k, tok in tokens.items():
        var = {"run_id": _ctx_run_id, "worker_id": _ctx_worker_id,
               "task_id": _ctx_task_id, "trace_id": _ctx_trace_id}.get(k)
        if var is not None:
            var.reset(tok)


class Progress:
    def __init__(self, total: int, desc: str = ""):
        self._pbar = None
        try:
            from tqdm.asyncio import tqdm  # type: ignore
            self._pbar = tqdm(total=total, desc=desc, dynamic_ncols=True, mininterval=0.5)
        except ImportError:
            pass
        self._lock = asyncio.Lock()

    async def update(self, n: int = 1, postfix: Optional[dict] = None):
        async with self._lock:
            if self._pbar:
                if postfix:
                    self._pbar.set_postfix(postfix, refresh=False)
                self._pbar.update(n)

    def close(self):
        if self._pbar:
            self._pbar.close()
```

### 公开 API 影响

无。仅新增模块。

### 风险点

- `contextvars` 在 Python 3.7+ 才有；AgentFlow 已要求 3.10+，OK。
- 多 logger 重复添加 handler：`_agentflow_installed` 标志位防止。

### 可验证测试

```bash
cd /home/yanguochen/workspace/AgentFlow
python -c "
from rollout.core.logging_utils import install_root_handler, get_logger, set_context
install_root_handler()
log = get_logger('test')
log.info('before context')
tok = set_context(run_id='r1', worker_id='w0', task_id='t42', trace_id='tr1')
log.info('inside context')
" 2>&1 | head -3
```

期望输出（时间戳忽略）：

```
[INFO] test run=- w=- task=- trace=- | before context
[INFO] test run=r1 w=w0 task=t42 trace=tr1 | inside context
```

---

## Commit 0.2: `feat(rollout): add ShutdownManager with signal handling`

### 动机

ENG-9。把 SIGINT/SIGTERM 显式注册到 event loop，第一次给"优雅退出"信号、二次警告、三次强退。配合 0.3 让 cleanup 在 timeout 内完成。

### 修改文件


| 文件                         | 类型  | 改动                                     |
| -------------------------- | --- | -------------------------------------- |
| `rollout/core/shutdown.py` | 新增  | `ShutdownManager`：注册 signal、暴露 `event` |


### 关键代码

```python
# rollout/core/shutdown.py
import asyncio
import os
import signal as _signal
from typing import Optional
from .logging_utils import get_logger

log = get_logger("rollout.shutdown")


class ShutdownManager:
    """Cooperative shutdown coordinator.

    Usage:
        sm = ShutdownManager()
        sm.install(asyncio.get_running_loop())
        ...
        if sm.event.is_set():
            ...  # graceful exit branch
    """

    def __init__(self, force_exit_after: int = 3):
        self.event = asyncio.Event()
        self._count = 0
        self._force_after = force_exit_after
        self._installed = False

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._installed:
            return
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main-thread
                _signal.signal(sig, lambda s, f: self._on_signal(s))
        self._installed = True

    def _on_signal(self, sig) -> None:
        self._count += 1
        if self._count == 1:
            log.warning(f"Received signal {sig}; graceful shutdown started "
                        f"(press Ctrl+C again to warn, x3 to force-exit)")
            self.event.set()
        elif self._count < self._force_after:
            log.warning(f"Signal {sig} received again ({self._count}/{self._force_after}); "
                        f"cleanup still in progress, press once more to force-exit")
        else:
            log.error(f"Force exit on signal {sig} (count={self._count})")
            os._exit(130)

    @property
    def triggered(self) -> bool:
        return self.event.is_set()
```

### 公开 API 影响

无。新增模块；下个 commit 接入 pipeline。

### 风险点

- 非主线程 / Windows 上 `loop.add_signal_handler` 不可用：fallback 到 `signal.signal`，在 main thread 仍能工作。
- 注册两次：`_installed` 标志保护。

### 可验证测试

```bash
python -c "
import asyncio
from rollout.core.shutdown import ShutdownManager

async def main():
    sm = ShutdownManager(force_exit_after=3)
    sm.install(asyncio.get_running_loop())
    print('installed; sending SIGINT to self in 0.1s')
    import os, signal, threading
    threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    await asyncio.wait_for(sm.event.wait(), timeout=2.0)
    print('event triggered, OK')

asyncio.run(main())
"
```

期望输出：

```
installed; sending SIGINT to self in 0.1s
[WARNING] rollout.shutdown ... Received signal ...
event triggered, OK
```

---

## Commit 0.3: `feat(rollout): cancel-safe pipeline shutdown + atomic result append`

### 动机

ENG-7（写盘交错）、ENG-9（双击 Ctrl+C SIGKILL）。

- `_save_result` 加 `asyncio.Lock` 并把 IO 通过 `asyncio.to_thread` 移出 event loop。
- pipeline 在 `run_async` 注入 `ShutdownManager`，主循环 `await wait(FIRST_COMPLETED, {workers, shutdown_event})`。
- 所有 cleanup（`runner.stop()`、`sandbox.close()`、`destroy_session`）包 `asyncio.shield + wait_for(timeout)`。

### 修改文件


| 文件                       | 类型  | 改动                                           |
| ------------------------ | --- | -------------------------------------------- |
| `rollout/pipeline.py`    | 修改  | `run_async`、`_save_result`、`_run_sequential` |
| `rollout/core/runner.py` | 修改  | `stop()` 改成 cancel-safe                      |


### 关键代码

```python
# rollout/pipeline.py（关键片段）
import asyncio
import json
import os
import uuid
from .core.logging_utils import install_root_handler, get_logger, set_context, clear_context
from .core.shutdown import ShutdownManager

log = get_logger("rollout.pipeline")


class RolloutPipeline:
    def __init__(self, config, output_dir=None):
        ...
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self._save_lock: Optional[asyncio.Lock] = None
        self._shutdown: Optional[ShutdownManager] = None

    async def run_async(self) -> RolloutSummary:
        install_root_handler(level=getattr(self.config, "log_level", "INFO"))
        tokens = set_context(run_id=self.run_id)
        try:
            self._save_lock = asyncio.Lock()
            self._shutdown = ShutdownManager()
            self._shutdown.install(asyncio.get_running_loop())

            if not self.benchmark_items:
                self.load_benchmark()

            runner = AgentRunner(self.config, worker_id="main_runner")
            try:
                ok = await runner.start()
                if not ok:
                    raise RuntimeError("Runner start failed")
                if self.config.parallel and self.config.concurrency > 1:
                    await self._run_parallel()  # 实现在 Phase 2
                else:
                    await self._run_sequential(runner)
            finally:
                # cancel-safe cleanup, max 30s
                try:
                    await asyncio.shield(
                        asyncio.wait_for(runner.stop(), timeout=self.config.shutdown_timeout)
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    log.warning("runner.stop() did not finish in shutdown_timeout; "
                                "session may be cleaned by server TTL")

            ... evaluate / summary ...
        finally:
            clear_context(tokens)

    async def _save_result(self, result: TaskResult) -> None:
        payload = self._build_payload(result)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._save_lock:
            await asyncio.to_thread(self._append_line_sync, line)

    def _append_line_sync(self, line: str) -> None:
        # atomic-ish append: write + flush + fsync
        with open(self.results_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
```

```python
# rollout/core/runner.py（关键片段）
async def stop(self) -> None:
    """Cancel-safe stop. Even if outer is cancelling, finish core cleanup."""
    try:
        if self.sandbox:
            # 1) 主动告诉 server 销毁本 worker 的 session（worker_id 范围）
            try:
                if self.config.resource_types:
                    await asyncio.wait_for(
                        self.sandbox.destroy_session(self.config.resource_types),
                        timeout=10.0,
                    )
            except Exception as e:
                log.warning(f"destroy_session failed during stop: {e}")
            # 2) 关 client（一定要做，否则 httpx 资源泄漏）
            try:
                await asyncio.wait_for(self.sandbox.close(), timeout=5.0)
            except Exception as e:
                log.warning(f"sandbox.close() failed: {e}")
            self.sandbox = None
    finally:
        self._started = False
        log.info(f"runner stopped (worker={self.worker_id})")
```

### 风险点

- `asyncio.shield` 防止外层 cancel 中断 cleanup；但如果 `wait_for` 超时，里面的协程会被 cancel；可以接受（最坏情况 session 等 TTL 清）。
- 双击 Ctrl+C 仍能跑 1.5 次 cleanup：第一次触发 shield 内 cleanup，第二次 print warn，第三次 `_exit(130)`。

### 可验证测试

1. **正常写入**：跑 5 题 sequential，jsonl 5 行可解析。
2. **kill -9 测试**：跑 100 题途中 `kill -9 $(pgrep -f rollout)`，已写入 jsonl 行 100% JSON valid。
  ```bash
   python -c "
   import json
   ok = bad = 0
   for line in open('results_xxx.jsonl'):
       try: json.loads(line); ok += 1
       except: bad += 1
   print(f'ok={ok} bad={bad}')
   "
  ```
   期望 `bad=0`。
3. **Ctrl+C 测试**：跑 50 题 sequential 中按 Ctrl+C，期望日志看到 "graceful shutdown started"，进程在 10s 内退出，server 端 `curl /api/v1/session/list` 看不到本次 run 的 session 残留。

---

## Commit 0.4: `feat(rollout): propagate trace_id and capture structured ToolCall fields`

### 动机

ENG-13。把 `ToolCall` 从只有 `result/success/error` 扩成完整结构化字段，并把 rollout 端生成的 `trace_id` 一路透传到 sandbox server，三处日志可对齐。

### 修改文件


| 文件                       | 类型  | 改动                                                                                                                   |
| ------------------------ | --- | -------------------------------------------------------------------------------------------------------------------- |
| `rollout/core/models.py` | 修改  | `ToolCall` 加 `formatted_result / code / message / resource_type / session_id / trace_id / execution_time_ms / error` |
| `rollout/core/runner.py` | 修改  | `_run_conversation` 中在记录 `ToolCall` 前从 raw response 抽取 `code/message/meta`                                           |
| `rollout/core/runner.py` | 修改  | `_execute_tool` 接受/生成 `trace_id` 并透传给 `sandbox.execute(..., trace_id=...)`                                           |
| `sandbox/sandbox.py`     | 修改  | `execute(..., trace_id=None)` → 透传到 `HTTPServiceClient.execute`                                                      |
| `sandbox/client.py`      | 修改  | `execute(..., trace_id=None)` → 写到 request body                                                                      |
| `sandbox/protocol.py`    | 修改  | `ExecuteRequest` 已经支持 `trace_id`（见 `routes.py` 第 71-73 行），确认 schema                                                  |


### 关键代码

```python
# rollout/core/models.py
@dataclass
class ToolCall:
    tool_name: str
    parameters: Dict[str, Any]
    # 给 LLM 的字符串（保持 result_formatter.py 契约）
    formatted_result: str = ""
    # 业务复盘用的原始 data
    result: Any = None
    # 状态字段（从 raw response 提取）
    success: bool = True
    code: Optional[int] = None
    message: str = ""
    error: Optional[str] = None
    resource_type: Optional[str] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    execution_time_ms: float = 0.0
```

```python
# rollout/core/runner.py（_run_conversation 关键片段）
import uuid
from .logging_utils import set_context, clear_context

async def _execute_tool(self, tool_name, parameters, *, trace_id, **kwargs):
    if not self.sandbox:
        raise RuntimeError("Sandbox not initialized")
    if kwargs:
        parameters = {**parameters, **kwargs}
    try:
        return await self.sandbox.execute(tool_name, parameters, trace_id=trace_id)
    except Exception as e:
        log.exception(f"tool execution error: {tool_name}")
        return {"code": -1, "message": str(e), "data": None, "meta": {}}


# in _run_conversation, for each tool call:
trace_id = f"{self.run_id}:{self.worker_id}:{task.id}:t{turn}:{tool_call.id}"
tokens = set_context(trace_id=trace_id)
try:
    raw = await self._execute_tool(tool_name, tool_args, trace_id=trace_id, **task_kwargs)
    formatted = format_tool_result_for_message(raw)

    code = raw.get("code") if isinstance(raw, dict) else None
    meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
    success = code == 0 if code is not None else True

    tc = ToolCall(
        tool_name=tool_name,
        parameters=tool_args,
        formatted_result=formatted,
        result=raw.get("data") if isinstance(raw, dict) else raw,
        success=success,
        code=code,
        message=raw.get("message", "") if isinstance(raw, dict) else "",
        error=None if success else raw.get("message", "tool failed"),
        resource_type=meta.get("resource_type"),
        session_id=meta.get("session_id"),
        trace_id=meta.get("trace_id") or trace_id,
        execution_time_ms=meta.get("execution_time_ms", 0.0),
    )
    trajectory.tool_calls.append(tc)
finally:
    clear_context(tokens)
```

### 风险点

- `sandbox.execute` / `HTTPServiceClient.execute` 已经有 `trace_id` 字段（`routes.py` 第 71-73 行能识别），客户端只需要在请求 body 里加。但要确认 `ExecuteRequest` schema 接受这个字段。
- 旧的 `trajectory.tool_calls[*]` schema 多了字段，向前兼容（jsonl 多字段无所谓）。

### 可验证测试

1. **trace_id 三处对齐**：跑 1 题，从 `results.jsonl` 拿到某次 tool call 的 `trace_id`，在 rollout worker log 和 sandbox server log 中能 grep 到同一个 trace_id。
2. **失败 tool call**：故意调一个不存在的 tool，`ToolCall.success=False, code!=0, message` 非空。
3. **schema 兼容**：用 `jq '.trajectory.tool_calls[0] | keys' results.jsonl`，能看到新字段。

---

## Commit 0.5: `feat(rollout): three-tier timeout (task / llm / tool)`

### 动机

ENG-10。当前没有任何超时，单题卡死时其他题全部卡。引入三层：

- **task 级**：`task_max_seconds`，包裹整个 `run_task`。
- **LLM 级**：`llm_timeout`，已有但未用，在 `async_chat_completion` 顶层加一次 `asyncio.wait_for`。
- **tool 级**：`tool_default_timeout` + 工具级 override（如 `vm:start` 给 120s）。

### 修改文件


| 文件                       | 类型  | 改动                                                                                      |
| ------------------------ | --- | --------------------------------------------------------------------------------------- |
| `rollout/core/config.py` | 修改  | 加 `task_max_seconds / tool_default_timeout / tool_timeout_overrides / shutdown_timeout` |
| `rollout/core/runner.py` | 修改  | `run_task` 包 `wait_for(task_max_seconds)`；`_execute_tool` 传 timeout 到 sandbox           |
| `rollout/core/utils.py`  | 修改  | `async_chat_completion` 顶层包 `wait_for(llm_timeout)`                                     |


### 关键代码

```python
# rollout/core/config.py（追加）
task_max_seconds: float = 1800.0       # 单题最长 30 分钟
tool_default_timeout: float = 60.0     # 一次 tool 调用默认 60s
tool_timeout_overrides: Dict[str, float] = field(default_factory=lambda: {
    "vm:start": 120.0,
    "browser:start": 60.0,
})
shutdown_timeout: float = 30.0         # cleanup 总预算
```

```python
# rollout/core/runner.py
async def run_task(self, task):
    timeout = self.config.task_max_seconds
    try:
        return await asyncio.wait_for(self._run_task_inner(task), timeout=timeout)
    except asyncio.TimeoutError:
        log.error(f"task timeout after {timeout}s")
        return TaskResult(
            task_id=task.id, question=task.question,
            predicted_answer="", ground_truth=task.answer,
            success=False, error=f"task_timeout_{int(timeout)}s",
            metadata=task.metadata,
        )

async def _execute_tool(self, tool_name, parameters, *, trace_id, **kwargs):
    timeout = self.config.tool_timeout_overrides.get(
        tool_name, self.config.tool_default_timeout)
    try:
        return await self.sandbox.execute(
            tool_name, parameters, trace_id=trace_id, timeout=timeout)
    except asyncio.TimeoutError:
        return {"code": -2, "message": f"tool_timeout_{int(timeout)}s",
                "data": None, "meta": {}}
```

```python
# rollout/core/utils.py
async def async_chat_completion(client, *, max_retries=3, retry_wait=0.5,
                                retry_backoff=2.0, llm_timeout=120.0, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=llm_timeout)
        except (asyncio.TimeoutError, Exception) as e:
            if attempt >= max_retries:
                raise
            wait = retry_wait * (retry_backoff ** attempt)
            log.warning(f"LLM call failed ({attempt+1}/{max_retries+1}): {e}; retry in {wait:.1f}s")
            await asyncio.sleep(wait)
```

### 风险点

- `asyncio.wait_for` 超时会把里面的协程 cancel，被 cancel 的 LLM 请求/HTTP 请求会被中断；这正是我们想要的（释放线程/连接）。
- task 超时后产生的 partial trajectory 仍然会写入 results（`success=False`），方便排查。

### 可验证测试

1. **task 超时**：构造一个 prompt 让 LLM 进入死循环（连续工具调用），设 `task_max_seconds=5`，task 5s 后返回 `success=False, error="task_timeout_5s"`。
2. **tool 超时**：调一个会阻塞 30s 的 mock tool，设 `tool_default_timeout=2`，2s 后 ToolCall `code=-2, message="tool_timeout_2s"`。
3. **LLM 超时**：把 `llm_timeout=0.001`，应在 1 次 retry 后失败。

---

## Phase 0 验收 checklist

- 日志带 `run_id / worker_id / task_id / trace_id`，可 grep。
- Ctrl+C 一次 → graceful，三次 → force exit。
- kill -9 后 jsonl 行 100% 可解析。
- 单题超时不影响其他题（串行模式下其他题继续跑）。
- rollout / sandbox 日志能用同一个 trace_id 关联。
- `trajectory.tool_calls[*]` 包含 `success/code/message/session_id/trace_id/execution_time_ms`。
- 旧配置文件无修改可跑通。

---

# Phase 1：LLM AsyncOpenAI 真异步化

**目标**：解掉 ENG-1。让 LLM 调用走真 async，并发上限脱离默认线程池 32 的约束。

---

## Commit 1.1: `feat(rollout): switch LLM client to AsyncOpenAI with httpx limits`

### 动机

ENG-1。具体诊断见 `plan.md` 0.3 节。当前 `async_chat_completion` 用同步 `openai.OpenAI` + `loop.run_in_executor(None, ...)`，被默认线程池封顶 32 路。改用 `openai.AsyncOpenAI` + 显式 `httpx.AsyncClient(limits=...)`。

### 修改文件


| 文件                       | 类型  | 改动                                                                              |
| ------------------------ | --- | ------------------------------------------------------------------------------- |
| `rollout/core/utils.py`  | 修改  | 新增 `create_async_openai_client`；重写 `async_chat_completion`                      |
| `rollout/core/runner.py` | 修改  | `start()` 用 `create_async_openai_client`；`stop()` 关 client                      |
| `rollout/core/config.py` | 修改  | 加 `llm_max_connections / llm_max_keepalive / llm_timeout / llm_connect_timeout` |


### 关键代码

```python
# rollout/core/utils.py
import httpx
import openai

def create_async_openai_client(
    api_key: str, base_url: str,
    max_connections: int = 256,
    max_keepalive: int = 64,
    timeout_s: float = 120.0,
    connect_timeout_s: float = 15.0,
) -> openai.AsyncOpenAI:
    if not api_key or not base_url:
        raise ValueError("Missing api_key/base_url")
    http = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
        ),
        timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
    )
    return openai.AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http)


async def async_chat_completion(
    client: "openai.AsyncOpenAI", *,
    max_retries: int = 3, retry_wait: float = 0.5,
    retry_backoff: float = 2.0, llm_timeout: float = 120.0,
    **kwargs,
):
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=llm_timeout)
        except Exception as e:
            if attempt >= max_retries:
                raise
            wait = retry_wait * (retry_backoff ** attempt)
            log.warning(f"LLM call failed ({attempt+1}/{max_retries+1}): {type(e).__name__}: {e}; retry in {wait:.1f}s")
            await asyncio.sleep(wait)
```

```python
# rollout/core/runner.py
self.client = create_async_openai_client(
    api_key=self.config.api_key,
    base_url=self.config.base_url,
    max_connections=self.config.llm_max_connections,
    max_keepalive=self.config.llm_max_keepalive,
    timeout_s=self.config.llm_timeout,
    connect_timeout_s=self.config.llm_connect_timeout,
)

# stop() 内：先 destroy session，再关 LLM client
try:
    await asyncio.wait_for(self.client.close(), timeout=5.0)
except Exception as e:
    log.warning(f"close LLM client failed: {e}")
```

### 公开 API 影响

- `async_chat_completion(client, ...)` 的 `client` 类型从 `openai.OpenAI` 变为 `openai.AsyncOpenAI`。仅 runner 内部使用，用户脚本不感知。

### 风险点

- `openai>=1.x` SDK 版本必须支持 `AsyncOpenAI`；在 `requirements.txt` 锁 `openai>=1.30,<2`。
- `AsyncOpenAI` 的 `tools=[...]` 参数序列化路径与 sync 一致；smoke test 验一次 tool calling。
- 关闭顺序：先 destroy session 再关 LLM client，避免 in-flight 请求挂死。

### 可验证测试

```bash
python -c "
import asyncio, time
from rollout.core.utils import create_async_openai_client, async_chat_completion

client = create_async_openai_client(api_key='...', base_url='https://...')

async def one():
    return await async_chat_completion(client, model='gpt-4o-mini',
        messages=[{'role':'user','content':'say ok'}], llm_timeout=30)

async def main():
    t0 = time.time()
    await asyncio.gather(*[one() for _ in range(50)])
    print(f'50 in {time.time()-t0:.2f}s')
    await client.close()

asyncio.run(main())
"
```

期望：50 并发的总时间 ≈ 单次时间（≈2-5s），不是 50× 单次（如果仍走默认线程池 32 应在 5-10s）。

---

## Commit 1.2: `refactor(rollout): keep sync chat_completion for evaluator path`

### 动机

`Evaluator._llm_judgement` 用同步 `chat_completion`，本 commit 保持其 sync 入口可用，便于 Phase 5 再单独做异步化。

### 修改文件


| 文件                          | 类型  | 改动                                                    |
| --------------------------- | --- | ----------------------------------------------------- |
| `rollout/core/utils.py`     | 修改  | `chat_completion` 保留同步实现 + 加 `print → log.warning`    |
| `rollout/core/evaluator.py` | 修改  | 显式调用 `chat_completion`（同步），不调 `async_chat_completion` |


### 风险点

- 同步 `chat_completion` 内部用 `openai.OpenAI`（不是 AsyncOpenAI），需要 `create_openai_client` 仍可用，作为兼容入口。**保留** `create_openai_client`，不删。

### 可验证测试

`llm_judgement` metric 跑 5 个样本，分数与改造前一致。

---

## Phase 1 验收 checklist

- `async_chat_completion` 的 `client` 已是 `AsyncOpenAI`。
- 50 并发跑同 base_url，总耗时 ≈ 单次耗时。
- evaluator 的 `llm_judgement` 仍能正常工作。
- `runner.stop()` 中显式关闭 LLM client。

---

# Phase 2：rollout worker-pool 调度

**目标**：解 ENG-2、ENG-3。把 `_run_parallel` 重写成 worker-pool：N 个 worker slot 各自独占 `worker_id + AgentRunner + sandbox session`，slot 内串行、slot 间并发。这是 v1.1 与 v1.0 的根本分歧点。

---

## Commit 2.1: `feat(rollout): add worker-pool config fields and validation`

### 动机

为 worker-pool 调度准备配置项。**先单独合并配置 commit**，让后续 commit 在 review 时可以单独看调度逻辑变更。

### 修改文件


| 文件                       | 类型  | 改动                                        |
| ------------------------ | --- | ----------------------------------------- |
| `rollout/core/config.py` | 修改  | 新增字段 + `validate()` 检查 + `from_dict` 兼容逻辑 |


### 关键代码

```python
# rollout/core/config.py（追加）
# === 并发调度 ===
concurrency: int = 1                  # worker 数（每 slot 独占 worker_id + session）
worker_startup_jitter: float = 3.0    # worker 启动随机抖动，避免风暴
worker_startup_batch_size: int = 0    # 0 = 不分批；>0 时每批 N worker
worker_startup_batch_interval: float = 5.0
fail_fast: bool = False
keep_results_in_memory: bool = True
serper_qps: int = 0                   # 0 = 不启用工具桶

# === 重试 ===
sandbox_retry_max: int = 3
sandbox_retry_backoff_base: float = 1.0  # 指数退避基数
sandbox_retry_jitter: float = 0.5

# === 超时（Phase 0 已加） ===
# task_max_seconds / tool_default_timeout / shutdown_timeout / llm_timeout
```

```python
# validate()
errors = []
if self.concurrency < 1:
    errors.append("concurrency must be >= 1")
if self.worker_startup_jitter < 0:
    errors.append("worker_startup_jitter must be >= 0")
if self.serper_qps < 0:
    errors.append("serper_qps must be >= 0")

# from_dict 兼容：旧配置只有 max_workers
if config_dict.get("concurrency", 1) == 1 and config_dict.get("max_workers", 1) > 1:
    logger.warning("Legacy max_workers detected; mapping to concurrency")
    self.concurrency = config_dict["max_workers"]
```

### 可验证测试

- 单测 `RolloutConfig.validate()`：合法/非法值。
- 旧配置文件零修改 load 通过。

---

## Commit 2.2: `feat(rollout): implement worker-pool scheduler in _run_parallel`

### 动机

ENG-2、ENG-3。重写 `_run_parallel`：

- 用 `asyncio.Queue` 装 task
- spawn N 个 `_worker(idx)` 协程
- 每个 worker 创建独立 `AgentRunner(worker_id="rollout_{run_id}_w{idx:03d}")`
- worker 内部 `while: queue.get_nowait() → run → save_result`
- 配合 ShutdownManager：worker 在 `_run_one_with_guard` 中检查 `shutdown.triggered`，触发后跳出循环

### 修改文件


| 文件                          | 类型     | 改动                                                                           |
| --------------------------- | ------ | ---------------------------------------------------------------------------- |
| `rollout/pipeline.py`       | 修改     | `_run_parallel` 重写；新增 `_spawn_worker` / `_run_one_with_guard` / `_on_result` |
| `rollout/core/scheduler.py` | 新增（可选） | `RolloutScheduler` Protocol + `WorkerPoolScheduler` 实现（未来切多进程时只换实现）          |


### 关键代码

```python
# rollout/pipeline.py
import random

async def _run_parallel(self) -> None:
    n = len(self.benchmark_items)
    queue: asyncio.Queue = asyncio.Queue()
    for item in self.benchmark_items:
        queue.put_nowait(item)
    progress = Progress(total=n, desc=f"rollout[c={self.config.concurrency}]")

    workers: List[asyncio.Task] = []
    for i in range(self.config.concurrency):
        workers.append(asyncio.create_task(
            self._spawn_worker(i, queue, progress), name=f"worker-{i:03d}"))

    shutdown_task = asyncio.create_task(self._shutdown.event.wait(), name="shutdown")

    try:
        done, pending = await asyncio.wait(
            workers + [shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # 如果是 shutdown 触发，主动 cancel 所有 worker
        if shutdown_task in done:
            log.warning(f"shutdown triggered; cancelling {len(workers)} workers")
            for w in workers:
                w.cancel()
        else:
            shutdown_task.cancel()
        # 等所有 worker 各自 finally 跑完（shutdown_timeout 控总时长）
        await asyncio.wait(workers, timeout=self.config.shutdown_timeout)
        # 兜底再 cancel 一次
        for w in workers:
            if not w.done():
                w.cancel()
    finally:
        progress.close()


async def _spawn_worker(self, idx: int, queue: asyncio.Queue, progress: Progress):
    worker_id = f"rollout_{self.run_id}_w{idx:03d}"
    tokens = set_context(worker_id=worker_id)
    # startup jitter
    if self.config.worker_startup_jitter > 0:
        await asyncio.sleep(random.uniform(0, self.config.worker_startup_jitter))
    if self.config.worker_startup_batch_size > 0:
        batch_idx = idx // self.config.worker_startup_batch_size
        await asyncio.sleep(batch_idx * self.config.worker_startup_batch_interval)

    runner = AgentRunner(self.config, worker_id=worker_id)
    try:
        ok = await runner.start()
        if not ok:
            log.error(f"worker[{worker_id}] start failed; aborting this worker")
            return
        while not self._shutdown.triggered:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            tokens_task = set_context(task_id=item.id)
            try:
                result = await self._run_one_with_guard(runner, item)
                self._maybe_keep_result(result)
                if self.config.save_results:
                    await self._save_result(result)
                await progress.update(postfix={
                    "ok": self._stats_ok,
                    "fail": self._stats_fail,
                })
            finally:
                clear_context(tokens_task)
                queue.task_done()
    except asyncio.CancelledError:
        log.warning(f"worker[{worker_id}] cancelled")
        raise
    finally:
        # cancel-safe cleanup
        try:
            await asyncio.shield(
                asyncio.wait_for(runner.stop(), timeout=self.config.shutdown_timeout))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning(f"worker[{worker_id}] runner.stop() didn't finish in time")
        clear_context(tokens)


async def _run_one_with_guard(self, runner, item) -> TaskResult:
    """Runner-level try/except + per-task timeout fallback."""
    try:
        return await runner.run_task(item)
    except asyncio.CancelledError:
        raise  # bubble up to worker
    except Exception as e:
        log.exception(f"task {item.id} unexpected error")
        return TaskResult(
            task_id=item.id, question=item.question,
            predicted_answer="", ground_truth=item.answer,
            success=False, error=str(e), metadata=item.metadata,
        )

def _maybe_keep_result(self, result):
    if self._stats_ok is None: self._stats_ok = 0
    if self._stats_fail is None: self._stats_fail = 0
    if result.success: self._stats_ok += 1
    else: self._stats_fail += 1
    if self.config.keep_results_in_memory:
        self.results.append(result)
```

### 风险点

- worker 顺序不保证：results 顺序也不保证（用 `task_id` 关联即可）。
- `keep_results_in_memory=False` 时 evaluator 必须从 results 文件回读（Phase 5 完整支持；Phase 3 之前不允许该模式 + `evaluate_results=True` 组合，在 `validate()` 拦截）。
- worker 启动失败：当前实现是 "abort this worker"，其他 worker 继续；可在 Phase 2S 加 fail_fast_workers 选项。

### 可验证测试

1. **回归测试**：`parallel=true, concurrency=1` 与 `parallel=false` 结果完全一致（同样 task 顺序、同样最终 results）。
2. **加速测试**：`concurrency=10` 跑 100 题（mock 每题 5s 的 sleep），总耗时 ≈ 50s（10×串行 1 题），不是 500s。
3. **隔离测试**：跑 GUI/VM benchmark `concurrency=4`，server 端 `curl /api/v1/session/list` 看到 4 个不同 `worker_id` 的 VM session。
4. **Ctrl+C 测试**：`concurrency=10` 跑到一半 Ctrl+C，10 个 worker 在 `shutdown_timeout` 内都退出，没有协程泄漏。
5. **单 worker 故障隔离**：手动 mock 让 worker_002 在 start 时 raise，其他 9 个 worker 继续完成。

---

## Commit 2.3: `feat(rollout): unique worker_id, per-worker logger, startup jitter`

### 动机

ENG-14（启动风暴）。把 worker_id 唯一化、每 worker 一个 logger 实例、`worker_startup_jitter` 落到实处。这一 commit 主要是把 2.2 中已有的部分逻辑显式化，并加 per-worker log 文件输出（可选）。

### 修改文件


| 文件                              | 类型  | 改动                                                 |
| ------------------------------- | --- | -------------------------------------------------- |
| `rollout/core/logging_utils.py` | 修改  | 加 `attach_worker_file_handler(worker_id, log_dir)` |
| `rollout/pipeline.py`           | 修改  | `_spawn_worker` 中调 `attach_worker_file_handler`    |
| `rollout/core/config.py`        | 修改  | 加 `log_dir / log_level / per_worker_log`           |


### 关键代码

```python
# rollout/core/logging_utils.py
from logging.handlers import RotatingFileHandler

def attach_worker_file_handler(worker_id: str, log_dir: str,
                               level: str = "INFO",
                               max_bytes: int = 100 * 1024 * 1024,
                               backups: int = 3) -> logging.Handler:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"rollout.worker.{worker_id}.log")
    h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                            encoding="utf-8")
    fmt = ("%(asctime)s [%(levelname)s] %(name)s "
           "run=%(run_id)s w=%(worker_id)s task=%(task_id)s trace=%(trace_id)s | %(message)s")
    h.setFormatter(logging.Formatter(fmt))
    h.addFilter(_ContextFilter())
    h.setLevel(getattr(logging, level.upper()))
    logging.getLogger().addHandler(h)
    return h
```

`pipeline._spawn_worker` 在 `set_context(worker_id=...)` 之后 attach；worker finally 中 detach。

### 风险点

- 100 worker × per-worker log = 100 个文件。Linux 默认 `ulimit -n=1024` 够。需要在文档里提醒用户检查 `ulimit`。
- 多进程日志（未来）：当前阶段单进程，handler lock 够；多进程时需要换 `QueueHandler`。

### 可验证测试

- `concurrency=4` 跑 20 题，`logs/<run_id>/` 下应有 1 个 master log + 4 个 worker log。每个 worker log 只看到该 worker 的 task。

---

## Commit 2.4: `feat(rollout): aggregate tool execution stats per task and summary`

### 动机

利用 0.4 commit 中已采集的 `ToolCall.success/code` 字段，在每条 task result 和最终 summary 中输出 tool execution stats，与 evaluator 的答案分数**分开展示**（避免口径混淆）。

### 修改文件


| 文件                       | 类型  | 改动                                          |
| ------------------------ | --- | ------------------------------------------- |
| `rollout/core/models.py` | 修改  | `TaskResult` 加 `tool_stats: Optional[Dict]` |
| `rollout/core/runner.py` | 修改  | `run_task` 返回前计算 `tool_stats`               |
| `rollout/pipeline.py`    | 修改  | `summary` 中聚合 `tool_stats`                  |


### 关键代码

```python
# rollout/core/runner.py
def _compute_tool_stats(trajectory):
    total = len(trajectory.tool_calls)
    success = sum(1 for tc in trajectory.tool_calls if tc.success)
    by_tool, by_code = {}, {}
    for tc in trajectory.tool_calls:
        bt = by_tool.setdefault(tc.tool_name, {"total":0, "success":0, "failed":0})
        bt["total"] += 1
        bt["success" if tc.success else "failed"] += 1
        if tc.code is not None:
            by_code[str(tc.code)] = by_code.get(str(tc.code), 0) + 1
    return {
        "total": total,
        "success": success,
        "failed": total - success,
        "success_rate": (success/total) if total else 0.0,
        "by_tool": by_tool,
        "by_code": by_code,
    }
```

### 可验证测试

- 跑一题包含 5 次 tool call（3 成功 2 失败），results jsonl 该条 `tool_stats.success_rate=0.6`，`by_code` 包含失败的 code 值。
- summary 中能看到全局 `tool_stats.success_rate`。

---

## Phase 2 验收 checklist

- `parallel=true, concurrency=1` 与 `parallel=false` 结果完全一致。
- `concurrency=10` 100 题（mock 5s）总耗时 ≈ 50s。
- server 端能看到 N 个不同 `worker_id` 的 session。
- Ctrl+C 在 `shutdown_timeout` 内退出，无协程泄漏。
- 每 worker 一个 log 文件。
- `tool_stats` 在 results 和 summary 中可见。

---

# Phase 2S：sandbox server 防风暴

**目标**：解 ENG-4、ENG-5、ENG-6、ENG-15、ENG-16。让 sandbox server 在 100 worker 同时压上来时不崩。

> **跑 Web/RAG 场景可以延后做**；跑 GUI/VM/Browser **必须**做（不做的话 ResourceRouter 全局锁会把 N 个 worker 启动串行化成 N×30s）。

---

## Commit 2S.1: `fix(sandbox): split ResourceRouter lock; init outside global lock`

### 动机

ENG-4。`ResourceRouter.get_or_create_session` 现在把 `await initializer(...)` 放在 `self._lock` 内（`resource_router.py` 第 191-243 行）。VM init 30s 期间 N 个 worker 全卡住，连 `/health` 都可能抖。

### 修改文件


| 文件                                       | 类型  | 改动                                                                                                                            |
| ---------------------------------------- | --- | ----------------------------------------------------------------------------------------------------------------------------- |
| `sandbox/server/core/resource_router.py` | 修改  | `get_or_create_session` 拆成三段：lock 内只标记 `initializing` → lock 外 `await initializer` → lock 内标记 `active`；加 per-key singleflight |


### 关键代码

```python
# sandbox/server/core/resource_router.py
class ResourceRouter:
    def __init__(self, session_ttl=300, auto_create=True):
        ...
        self._routes_lock = asyncio.Lock()
        # singleflight: (worker_id, resource_type) -> Future
        self._initializing: Dict[Tuple[str, str], asyncio.Future] = {}

    async def get_or_create_session(self, worker_id, resource_type, config=None, auto_created=False, custom_name=None):
        key = (worker_id, resource_type)
        # Step 1: fast path under lock
        async with self._routes_lock:
            if worker_id in self._routes and resource_type in self._routes[worker_id]:
                info = self._routes[worker_id][resource_type]
                info["last_activity"] = datetime.utcnow().isoformat()
                info["expires_at"] = (datetime.utcnow() + timedelta(seconds=self._session_ttl)).isoformat()
                return info
            # singleflight: another caller is initializing
            if key in self._initializing:
                fut = self._initializing[key]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._initializing[key] = fut

        # if we're not the leader, just wait
        if fut.done() is False and fut is not self._initializing.get(key):
            return await fut

        # Step 2: do heavy init OUTSIDE the global lock
        try:
            session_id = f"{resource_type}_{worker_id}_{uuid.uuid4().hex[:8]}"
            init_config = self._merge_resource_config(resource_type, config)
            info = self._make_initial_info(worker_id, resource_type, session_id, init_config, auto_created, custom_name)
            initializer = self._resource_initializers.get(resource_type)
            if initializer:
                if asyncio.iscoroutinefunction(initializer):
                    init_result = await initializer(worker_id, init_config)
                else:
                    init_result = await asyncio.to_thread(initializer, worker_id, init_config)
                if init_result:
                    info["data"].update(init_result)
                info["status"] = "active"
            else:
                info["status"] = "active"
                info["compatibility_mode"] = True
        except Exception as e:
            info = {"session_id": None, "status": "error", "error": str(e),
                    "worker_id": worker_id, "resource_type": resource_type}
            logger.error(f"[{worker_id}] resource init failed: {resource_type} - {e}")

        # Step 3: publish result under lock
        async with self._routes_lock:
            if info.get("status") == "active":
                self._routes.setdefault(worker_id, {})[resource_type] = info
            fut.set_result(info)
            self._initializing.pop(key, None)
        return info
```

### 风险点

- 同一 `(worker_id, resource_type)` 并发 `create_session` 走 singleflight，**第二个等第一个**，符合预期。
- `status/list/destroy/refresh/heartbeat` 仍走 `_routes_lock`，但这些都是 ms 级操作，不被 init 阻塞。
- `cleanup_expired` 周期任务也只在 lock 内做表读写。

### 可验证测试

1. **并行 init 测试**：mock 一个 RAG initializer sleep 3s，10 worker 同时 `get_or_create_session("rag")`，总耗时应 ≤ 4s（并行而非 30s 串行）。
2. **singleflight 测试**：同一 `worker_id` 同 `resource_type` 并发调 2 次 `get_or_create_session`，第二次不重复 init（initializer 调用次数 = 1）。
3. **lock 不阻塞 health**：`get_or_create_session` 期间持续打 `/health` 1000 RPS，p99 < 50ms。

---

## Commit 2S.2: `feat(sandbox): tiered backpressure (health/status/session/tool)`

### 动机

ENG-14、ENG-15。把入口分层，配独立 BoundedSemaphore，过载快速返回 `429` + `Retry-After`，不无界排队。

### 修改文件


| 文件                                    | 类型  | 改动                                                                       |
| ------------------------------------- | --- | ------------------------------------------------------------------------ |
| `sandbox/server/core/backpressure.py` | 新增  | `BoundedSemaphore` 包装、`Retry-After` 工具、统计                                |
| `sandbox/server/routes.py`            | 修改  | 各 endpoint 套对应的 semaphore；过载快速 429/503                                   |
| `sandbox/server/config_loader.py`     | 修改  | 配置项：`limits.global_inflight / limits.session_create / limits.tool / ...` |


### 关键代码

```python
# sandbox/server/core/backpressure.py
import asyncio, time
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse


class Bound:
    def __init__(self, name: str, capacity: int, queue_max: int = 0):
        self.name = name
        self.sem = asyncio.Semaphore(capacity)
        self.queue_max = queue_max
        self.waiters = 0

    @asynccontextmanager
    async def acquire_or_429(self, retry_after_s: float = 1.0):
        if self.queue_max > 0 and self.waiters >= self.queue_max:
            raise OverloadedError(self.name, retry_after_s)
        self.waiters += 1
        try:
            t0 = time.monotonic()
            await self.sem.acquire()
            wait_ms = (time.monotonic() - t0) * 1000
        finally:
            self.waiters -= 1
        try:
            yield {"queue_wait_ms": wait_ms}
        finally:
            self.sem.release()


class OverloadedError(Exception):
    def __init__(self, lane: str, retry_after_s: float):
        self.lane = lane
        self.retry_after_s = retry_after_s


def overloaded_response(err: OverloadedError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(max(1, int(err.retry_after_s)))},
        content={"code": 429, "message": f"overloaded:{err.lane}",
                 "data": None, "meta": {"retry_after_s": err.retry_after_s}},
    )
```

```python
# sandbox/server/routes.py（关键示意）
limits = server.backpressure  # 在 app.py 初始化

@app.post(HTTPEndpoints.EXECUTE)
async def execute_action(request: ExecuteRequest):
    try:
        async with limits.tool[request.resource_type or "default"].acquire_or_429(1.0):
            ...
    except OverloadedError as e:
        return overloaded_response(e)
```

```python
# 默认限额（在 config_loader 中可被覆盖）
DEFAULT_LIMITS = {
    "global_inflight": 512,
    "health_inflight": 256,
    "status_inflight": 128,
    "session_create": {"vm": 2, "browser": 4, "rag": 8, "default": 16},
    "tool": {"vm": 1, "browser": 1, "rag": 64, "websearch": 16, "default": 32},
}
```

### 风险点

- `/health` 必须**完全不读 session 表**，只检查进程是否在跑。现行 `health_check` 已经是这样。
- `/ready` 可缓存 5s TTL。
- websearch tool 之前每次 new 一个 ThreadPoolExecutor，限流不只看 Semaphore，还要看 2S.4 的共享线程池。

### 可验证测试

1. **health 不被阻塞**：`session_create` 占满情况下，`/health` p99 < 20ms。
2. **过载 429**：把 `session_create.vm=1`，2 个 worker 同时 `create_session("vm")`，第二个收到 429 + `Retry-After`。
3. **queue_wait_ms 出现在日志中**：在 server log 中能看到 `queue_wait_ms>0` 的样例。

---

## Commit 2S.3: `feat(sandbox): per-(worker_id, resource_type) tool serial lock`

### 动机

ENG-3 的服务端兜底。即便客户端用了 worker-pool（保证不共享 worker_id），仍可能因为客户端 bug 让同一 worker_id 下并发发起多个 tool call。VM/Browser/Bash 需要 server 端**强制** session 内串行。

### 修改文件


| 文件                                     | 类型  | 改动                                                                                        |
| -------------------------------------- | --- | ----------------------------------------------------------------------------------------- |
| `sandbox/server/core/tool_executor.py` | 修改  | 引入 `session_locks: Dict[(worker_id, resource_type), asyncio.Lock]`；按 resource_type 决定是否启用 |


### 关键代码

```python
# tool_executor.py
class ToolExecutor:
    def __init__(self, ...):
        ...
        self._session_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._serial_resources = {"vm", "browser", "bash", "code", "mcp"}

    async def execute(self, action, params, *, worker_id, **kwargs):
        resource_type = self._tool_resource_types.get(action)
        if resource_type in self._serial_resources and worker_id:
            key = (worker_id, resource_type)
            lock = self._session_locks.setdefault(key, asyncio.Lock())
            async with lock:
                return await self._execute_inner(action, params, worker_id=worker_id, **kwargs)
        return await self._execute_inner(action, params, worker_id=worker_id, **kwargs)
```

### 风险点

- 锁泄漏：当 session destroy 时要 `_session_locks.pop(key, None)`，否则长跑会累积。
- RAG 不能用串行锁（QueryBatcher 需要并发进入）。

### 可验证测试

- 同 worker_id 的 `vm:click` × 5 并发，按时间戳验证是串行执行。
- 不同 worker_id 的并发不受影响。

---

## Commit 2S.4: `fix(sandbox): shared websearch thread pool + httpx limits + heartbeat jitter`

### 动机

ENG-5、ENG-6。

- websearch 每次调用 new `ThreadPoolExecutor(5)` → 100 worker × 5 = 500 线程瞬时。
- 100 worker 默认 30s 心跳齐步，server 端周期性尖峰。

### 修改文件


| 文件                                           | 类型  | 改动                                                                                    |
| -------------------------------------------- | --- | ------------------------------------------------------------------------------------- |
| `sandbox/server/backends/tools/websearch.py` | 修改  | 共享 `ThreadPoolExecutor`（server 启动时创建，所有 search 调用复用）                                  |
| `sandbox/client.py`                          | 修改  | `HTTPServiceClient.connect()` 加 `httpx.Limits(...)`；`_heartbeat_loop` 加 `±20%` jitter |


### 关键代码

```python
# websearch.py（关键修改）
class SearchTool(BaseTool):
    _shared_executor = None  # class-level

    @classmethod
    def _get_executor(cls, max_workers: int):
        if cls._shared_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            cls._shared_executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="websearch")
        return cls._shared_executor

    async def execute(self, query, **kwargs):
        ...
        executor = self._get_executor(self.get_config('max_workers', 8))
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(executor, client.search_single, q) for q in query]
        results = await asyncio.gather(*futures, return_exceptions=True)
        ...
```

```python
# sandbox/client.py
import random

async def connect(self):
    if self._client is None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.config.timeout,
            limits=httpx.Limits(
                max_connections=self.config.max_connections,  # default 64
                max_keepalive_connections=self.config.max_keepalive_connections,  # default 16
            ),
            headers={...},
        )
    ...

async def _heartbeat_loop(self):
    while not self._closed:
        try:
            base = self.config.heartbeat_interval
            # ±20% jitter
            wait = base * (0.8 + 0.4 * random.random())
            await asyncio.sleep(wait)
            await self._send_heartbeat()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"heartbeat failed: {e}")
```

### 可验证测试

- 100 并发触发 `web:search`，server `ps -eLf | grep websearch | wc -l` ≤ 8（共享池 max_workers），不是 500。
- server log 中 heartbeat 时间戳分散，不齐步。

---

## Commit 2S.5: `feat(sandbox): worker_disconnect on shutdown + server SIGTERM cleanup`

### 动机

ENG-16。rollout shutdown 时主动告诉 server 销毁本 run 的 session，不用等 5min TTL。server 进程 SIGTERM 时也要遍历清理 VM/Browser，避免残留。

### 修改文件


| 文件                              | 类型  | 改动                                                                        |
| ------------------------------- | --- | ------------------------------------------------------------------------- |
| `rollout/core/runner.py`        | 修改  | `stop()` 中 `sandbox.close(destroy_sessions=True)`（或显式调 worker_disconnect） |
| `sandbox/server/app.py`         | 修改  | FastAPI `lifespan` 的 `shutdown` 阶段遍历清理                                    |
| `sandbox/cli/sandbox-server.py` | 修改  | 注册 SIGINT/SIGTERM handler，让 uvicorn 走 graceful shutdown                   |


### 关键代码

```python
# rollout/core/runner.py（在 0.3 commit 基础上加强）
async def stop(self):
    try:
        if self.sandbox:
            try:
                # 主动一次性 disconnect 该 worker_id 的所有 session
                await asyncio.wait_for(
                    self.sandbox.close(destroy_sessions=True),
                    timeout=15.0,
                )
            except Exception as e:
                log.warning(f"sandbox close+destroy failed: {e}")
            self.sandbox = None
        if self.client:
            try:
                await asyncio.wait_for(self.client.close(), timeout=5.0)
            except Exception as e:
                log.warning(f"close LLM client failed: {e}")
            self.client = None
    finally:
        self._started = False
```

```python
# sandbox/server/app.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # shutdown
    logger.info("server shutting down; cleaning all sessions")
    try:
        await asyncio.wait_for(
            server.resource_router.destroy_all_sessions(),
            timeout=60.0,
        )
    except Exception as e:
        logger.error(f"shutdown cleanup failed: {e}")
```

### 可验证测试

1. **rollout 退出后 session 立刻没**：`concurrency=4` 启动 → 等 30s → Ctrl+C → server 端 `/api/v1/session/list` 立刻为空（不是等 5min）。
2. **server SIGTERM**：`kill -TERM <server_pid>`，server 在 60s 内退出，且日志显示 "cleaning all sessions"，无 VM/Browser 残留进程。

---

## Phase 2S 验收 checklist

- `/health` 在 1000 QPS 探活下 p99 < 20ms（即便 session create 占满）。
- 10 worker 同时 `create_session("rag")` 总时长 ≤ 单个 init 时长 × 1.5（并行而非串行）。
- session_create 队列满时返回 `429 + Retry-After`。
- 同 worker_id 的 VM/Browser/Bash/Code/MCP tool call 严格串行。
- websearch 100 并发时 server 线程数 ≤ 配置上限。
- heartbeat 时间戳分散无尖峰。
- rollout 退出后 server session 立刻清。
- server SIGTERM 时主动清 VM/Browser。

---

# Phase 3：断点续推

**目标**：解 ENG-11、ENG-12、ENG-18、ENG-20。100 并发跑 1 万题挂了能续。

---

## Commit 3.1: `feat(rollout): output filename strategy + file lock`

### 动机

ENG-20、ENG-11 前置。把 `results_<bench>_<ts>.jsonl` 命名改为可选 `stable` 命名，并加 fcntl 文件锁防双进程并发写。

### 修改文件


| 文件                             | 类型  | 改动                                                                                          |
| ------------------------------ | --- | ------------------------------------------------------------------------------------------- |
| `rollout/core/config.py`       | 修改  | 加 `output_filename_strategy / output_filename / resume / resume_file / resume_retry_failed` |
| `rollout/pipeline.py`          | 修改  | `__init_`_ 中根据 strategy 决定文件名；加 fcntl flock                                                 |
| `rollout/core/result_store.py` | 新增  | `ResultStore` 接口（read/write/lock）                                                           |


### 关键代码

```python
# rollout/core/config.py
output_filename_strategy: str = "timestamp"  # timestamp | stable | explicit
output_filename: Optional[str] = None
resume: bool = False
resume_file: Optional[str] = None
resume_retry_failed: bool = True
```

```python
# rollout/core/result_store.py
import fcntl, os

class ResultStore:
    def __init__(self, path: str):
        self.path = path
        self._lock_path = path + ".lock"
        self._lock_fd: Optional[int] = None

    def acquire_lock(self) -> None:
        self._lock_fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd); self._lock_fd = None
            raise RuntimeError(f"Another process holds {self._lock_path}; refuse to write")

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            try: fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def append_line(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line); f.flush(); os.fsync(f.fileno())

    def iter_lines(self):
        if not os.path.exists(self.path): return
        with open(self.path, "r", encoding="utf-8") as f:
            for ln in f: yield ln
```

### 风险点

- Windows 上 `fcntl` 不可用：在 `ResultStore.__init__` 中检测平台，Windows 用 `portalocker`（可选依赖）或退化为"无锁 + 警告"。

### 可验证测试

- 同时启动两个 rollout 进程写同一 stable 文件，第二个应快速 raise + 提示锁路径。

---

## Commit 3.2: `feat(rollout): resume by task_id with failure classification`

### 动机

ENG-11、ENG-12。

### 修改文件


| 文件                       | 类型  | 改动                                                                                                                                                       |
| ------------------------ | --- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rollout/pipeline.py`    | 修改  | `_load_completed_task_ids` + `load_benchmark` 后过滤 + 失败分类字段写入                                                                                             |
| `rollout/core/models.py` | 修改  | `TaskResult` 加 `task_status / task_fail / failure_stage / failure_type / failure_message / failed_turn / failed_tool_name / failed_trace_id / retryable` |
| `rollout/core/runner.py` | 修改  | `run_task` 异常路径填充失败分类字段                                                                                                                                  |


### 关键代码

```python
# pipeline.py
def _load_completed_task_ids(self) -> Set[str]:
    done = set()
    if not os.path.exists(self.results_file): return done
    for ln in self._store.iter_lines():
        try:
            obj = json.loads(ln)
            tid = obj.get("task_id")
            if not tid: continue
            if obj.get("success", False) or not self.config.resume_retry_failed:
                done.add(tid)
        except json.JSONDecodeError:
            log.warning("skip malformed jsonl line")
    return done
```

```python
# runner.py（异常路径）
return TaskResult(
    task_id=task.id, ...,
    success=False,
    task_status="failed",
    task_fail=True,
    failure_stage=self._classify_stage(e),
    failure_type=type(e).__name__,
    failure_message=str(e)[:500],
    failed_turn=turn_count,
    failed_tool_name=last_tool_name,
    failed_trace_id=last_trace_id,
    retryable=isinstance(e, (asyncio.TimeoutError, httpx.HTTPError)),
)
```

### 可验证测试

1. 跑 10 题，3 题失败。重启 `resume=true, resume_retry_failed=true`，应只跑 3 题。
2. `resume_retry_failed=false`：跳过所有已完成（含失败）。
3. `jq 'select(.task_fail==true)'` 能筛出失败样例。

---

## Commit 3.3: `feat(rollout): checkpoint mid-task trajectories (optional)`

### 动机

ENG-18。

### 修改文件


| 文件                                 | 类型  | 改动                                                            |
| ---------------------------------- | --- | ------------------------------------------------------------- |
| `rollout/core/checkpoint_store.py` | 新增  | `CheckpointStore.write_atomic / load_index / clear_completed` |
| `rollout/pipeline.py`              | 修改  | `__init__` 决定 `checkpoint_dir`；resume 时跳过 vs 续跑               |
| `rollout/core/runner.py`           | 修改  | 每个 turn / tool_result 完成后 `await checkpoint_store.write(...)` |


详见 `plan.md` 6.4.1–6.5.1 节。

### 可验证测试

- kill -9 后 `checkpoints/<run_id>/tasks/<task_id>.json` 存在且 JSON 完整。
- `resume_from_checkpoint=true`：能从 `after_tool_result` safe_point 继续推进。
- 默认 `resume_from_checkpoint=false`：有 checkpoint 但无 results 的 task 从头重跑。

---

## Phase 3 验收 checklist

- kill -9 重启后 `skip N done`。
- `task_status / task_fail / failure_stage` 在 jsonl 中可见。
- 双进程并发启动第二个 fail-fast。
- resume 后 evaluator 拿全量 results。
- checkpoint（如启用）能正确续跑。

---

# Phase 4：配置示例 + 调优文档

---

## Commit 4.1: `docs(infer): add parallel sample configs and tuning guide`

### 修改文件


| 文件                                      | 类型                   |
| --------------------------------------- | -------------------- |
| `configs/infer/web_infer.parallel.json` | 新增                   |
| `configs/infer/rag_infer.parallel.json` | 新增                   |
| `configs/infer/gui_infer.parallel.json` | 新增（小 concurrency 示例） |
| `docs/zh-CN/guides/PARALLEL_INFER.md`   | 新增（用户视角）             |
| `README_zh.md`                          | 修改                   |


### 推荐配置（落到 sample）

**Web（受 Serper/Jina 限速）**：

```json
{
  "parallel": true,
  "concurrency": 16,
  "worker_startup_jitter": 1.0,
  "llm_max_connections": 64,
  "llm_max_keepalive": 32,
  "llm_timeout": 120,
  "task_max_seconds": 600,
  "tool_default_timeout": 60,
  "sandbox_timeout": 60,
  "shutdown_timeout": 30,
  "serper_qps": 30,
  "fail_fast": false,
  "resume": true,
  "output_filename_strategy": "stable"
}
```

**RAG（QueryBatcher 友好）**：

```json
{
  "parallel": true,
  "concurrency": 96,
  "worker_startup_jitter": 3.0,
  "llm_max_connections": 256,
  "llm_max_keepalive": 64,
  "llm_timeout": 180,
  "task_max_seconds": 900,
  "tool_default_timeout": 30,
  "sandbox_timeout": 60,
  "shutdown_timeout": 30,
  "resume": true,
  "output_filename_strategy": "stable"
}
```

**GUI/VM（物理资源限制）**：

```json
{
  "parallel": true,
  "concurrency": 8,
  "worker_startup_jitter": 10.0,
  "worker_startup_batch_size": 4,
  "worker_startup_batch_interval": 30,
  "llm_max_connections": 32,
  "llm_timeout": 180,
  "task_max_seconds": 1800,
  "tool_default_timeout": 120,
  "tool_timeout_overrides": {"vm:start": 300, "vm:screenshot": 30},
  "sandbox_timeout": 120,
  "shutdown_timeout": 60,
  "fail_fast": false,
  "resume": true,
  "output_filename_strategy": "stable"
}
```

### 调优排查表

见 `plan.md` 7.4 节。可直接复用并补充：


| 现象              | 大概率原因                           | 排查                                                  |
| --------------- | ------------------------------- | --------------------------------------------------- |
| 100 并发实际跑 32    | Phase 1 没做（仍是伪 async）           | 确认 `AsyncOpenAI`                                    |
| GUI session 状态乱 | worker_id 共享                    | 确认 Phase 2.3，每 worker 唯一 ID                         |
| 启动 5 分钟         | Phase 2S.1 没做（锁未拆）              | 应用 2S.1                                             |
| 502/timeout 频发  | server 过载                       | 看 server `queue_wait_ms`；调小 `concurrency` 或加 limits |
| Ctrl+C 卡住       | shutdown_timeout 太短或 cleanup 死锁 | 调大 `shutdown_timeout`；检查 finally 是否 cancel-safe     |


---

## Phase 4 验收 checklist

- 三份 sample 端到端可跑（各 50 题 smoke test）。
- 文档与配置项命名一致。
- README 链接通。

---

# Phase 5：评测分类 / 异步化（可选）

---

## Commit 5.1: `feat(eval): EvaluationContext + offline/online_env modes`

### 动机

ENG-17。

### 修改文件 / 关键改动

详见 `plan.md` 8.3.1–8.3.3 节。

- `EvaluationContext`（task / result / sandbox / worker_id / session_ids / artifacts）
- `BaseEvaluator.mode: Literal["offline", "online_env", "mixed"]`
- pipeline 在 worker 内 task 完成后、session 销毁前调 `evaluate_one_async`

### 可验证测试

- 构造一个 mock terminal evaluator，确认它能在 session 存活时读到状态。

---

## Commit 5.2: `feat(eval): evaluate_async + evaluation cache for resume`

### 动机

ENG-19。

### 修改文件 / 关键改动

详见 `plan.md` 8.3.2 节。

### 可验证测试

- `evaluate` 与 `evaluate_async` 同 results 文件分数 diff = 0。
- 删一半 `evaluation_<bench>.jsonl` 重跑，只补缺。

---

## Phase 5 验收 checklist

- `mode=offline` evaluator 不访问 live sandbox。
- `mode=online_env` 在 session 销毁前完成。
- `mode=mixed` 能按 task_id 聚合到同一 summary。
- `keep_results_in_memory=False` 离线评测从 jsonl 回读。

---

# 9. FAQ：实施期常见问题

### Q1: 全部计划条目都要做吗？

不。v2.2 后不再用“25 个 / 必做 18 个”的旧口径。v2.3 已确认本轮覆盖 Web/RAG + GUI/VM/Browser，因此最小集是主线 Phase 0/1/2/2S/3.1/3.2/4，加上 §12.0.1 标为 P0/P1 且会影响协议或观测的小补丁（尤其 0.4a、0.4b、0.4c-BdbQuit、0.4d、0.4f、0.8a），以及随 Phase 2S 一起做的 0.7a/0.7b。Phase 3.3、Phase 5、P3 审计补丁仍按场景推进。

### Q2: 单 commit 能在一天内做完吗？

绝大多数 commit < 200 LoC，半天可写完 + 测试。Phase 2.2（worker-pool 调度）和 Phase 2S.1（锁拆分）相对复杂，建议各留 1-2 天。

### Q3: 应该按顺序做还是并行做？

- Phase 0 / Phase 1 必须按编号顺序（commit 0.3 用到 0.1 的 logger、0.2 的 ShutdownManager）。0.7c 与 0.3 同区块处理，避免两次改 `_save_result`。
- §12 P0/P1 小补丁在 Phase 0 后、Phase 2 前完成；0.4b 涉及 sandbox heartbeat 语义，随 Phase 2S.4 的 heartbeat jitter 一起做。
- Phase 2 在 Phase 0/1 与 0.8a 合并后才能做。
- **Phase 2S 是本轮必做**，建议在 Phase 2 smoke test 之后立即做，并把 0.4b/0.7a/0.7b 同批处理。
- Phase 2S 与 Phase 3 可以并行开发（Phase 2S 改 sandbox，Phase 3 改 rollout，物理上不冲突），但 GUI/VM/Browser smoke test 需要等 Phase 2S 合并后再作为上线门禁。
- Phase 4/5 必须在前面 Phase 稳定后做。

### Q4: 每个 commit 都要写完整单测吗？

不强制 unittest 框架，但**每个 commit 必须附带一个可执行的验证脚本**（在本文档的"可验证测试"段落里）。通过这些脚本 + 端到端 smoke test 覆盖即可。完整单元测试可以在 Phase 4 之后补，不阻塞合并。

### Q5: 如何回滚某个 commit？

主线目标是向后兼容，但 §12 的审计补丁里有少数可见行为变化，必须在对应 commit 的风险点里单独说明：

- Phase 0：删除 logger handler 即可退回 print（虽然几乎没意义）。
- Phase 1：恢复 `create_openai_client`（同步）即可。
- Phase 2：`parallel=false` 或 `concurrency=1` 即可走串行。
- Phase 2S：每个 limits 设为 INF 即可关闭。
- Phase 3：`resume=false, output_filename_strategy=timestamp` 退回旧行为。
- 审计补丁：如 duplicate task_id 默认 error、`Sandbox.close()` 默认销毁 session、`extract_final_answer` 取最后匹配、LLM judge JSON mode，均不能简单视为零行为变化。

### Q6: 如何处理多机部署？

本计划只覆盖单机多 worker。多机请：

- `output_filename_strategy=explicit`，每机分配不同文件名，跑完再 merge。
- sandbox server 每机独立部署（如果是有状态资源）。
- 多机协调不在本计划范围。

### Q7: 如何处理 ulimit?

100 worker 单进程会撞文件句柄上限。建议在 `pipeline.run_async()` 入口 print 一次 `ulimit -n` 当前值，< 4096 时 warn 提示用户调大。

```python
import resource
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if soft < 4096:
    log.warning(f"RLIMIT_NOFILE={soft} may be too low for concurrency={N}; "
                f"recommend `ulimit -n 65536`")
```

### Q8: Ctrl+C 之后已经 in-flight 的 task 会丢吗？

- 当前 result 已经 append 到 jsonl 的 task：不丢。
- 当前正在跑、还没 append 的 task：会被 cancel，下次 `resume=true` 时会重跑（除非 `resume_retry_failed=false`）。
- 半成品 trajectory（如果开了 Phase 3.3 checkpoint）：可从 safe_point 续跑。

### Q9: 如何监控 100 并发跑的健康度？

- rollout master log 的 progress 行（每 5s 一次 ok/fail）。
- server log 的 `queue_wait_ms`（高 = 拥堵）。
- worker log 中单 worker 的吞吐节奏。
- 可选：暴露 Prometheus 指标（不在本计划范围，但接口已留好）。

### Q10: 文档与代码不同步怎么办？

每个 commit 的描述里强制要求"如果改了配置项，必须同步改 `configs/infer/*.parallel.json` 和 `PARALLEL_INFER.md`"。CR 时 reviewer 检查。

---

# 10. 验收与上线

## 10.1 分阶段验收

每个 Phase 合并前满足该 Phase 末尾的 checklist。

## 10.2 端到端上线测试

在所有必做 commit 完成后，跑两组测试：

1. **小规模回归**：`web_infer.parallel.json` 配置 `concurrency=4` 跑 50 题。要求：
  - results jsonl 50 行可解析、`task_fail` 字段齐全。
  - 总耗时是串行模式的 ~25% 左右（考虑 LLM/API 限速）。
  - Ctrl+C 15s 内退出。
  - server `/api/v1/session/list` 退出后立即空。
2. **大规模压测**：`rag_infer.parallel.json` 配置 `concurrency=64` 跑 5000 题。要求：
  - server 进程内存稳定（< 8GB），不持续增长。
  - jsonl 续推 3 次（每次跑到 1/3 处 kill -9）都能正常 resume。
  - 最终评分与串行模式 diff < 0.5%（容许 LLM 非确定性差异）。

## 10.3 上线 checklist

- `requirements.txt` 锁版本（`openai`, `httpx`, `fastapi` 等）。
- CI 增加 `parallel=true, concurrency=2` 的 smoke test。
- 文档（PARALLEL_INFER.md）reviewer 是实际跑过的人。
- 默认配置仍保持 `parallel=false`（防止用户无感知切并发）。
- `ulimit -n` 在文档中显式提醒。

---

# 11. 维护与迭代

- 本文档是 v2.2 implementation plan。每个 commit 合并后，在第 0 节的 commit 表里加 `[done @ <hash>]` 标记。
- 任何配置字段增删改，必须同步：
  - `rollout/core/config.py`
  - `configs/infer/*.parallel.json`
  - `docs/zh-CN/guides/PARALLEL_INFER.md`
  - 本文档 ENG 编号映射表
- 如发现新的 ENG-N 工程坑：先在第 2 节登记，再分配 commit。
- 如发现某个 commit 实施时与本文档不符，**先更新本文档再改代码**。

---

# 12. 审计补丁系列（与 Commit 0.4 同性质的隐藏问题）

> 本章节是 2026-05-18 在写完 Commit 0.4 后，对全仓库做"同性质审计扫描"产出的补丁清单（ENG-21～34）。Commit 0.4 解决的是"sandbox 已给但 rollout 丢失"的一种症状；本章罗列的 14 个 commit 都是**同一类**问题：
>
> - 字段定义了但没填
> - 设计接好了但调用方没接上
> - 默认值埋雷
> - 信息已经产生但被压扁丢弃
>
> 每个 commit 都遵循"零或最小行为变更、独立可合并、有可验证测试"的标准。

## 12.0 汇总表（按 v2.2 优先级）


| Pri | Commit | 主题                                            | ENG    | 时机 / 备注 |
| --- | ------ | --------------------------------------------- | ------ | -------- |
| P0  | 0.4c-a | `BdbQuit` 不被 `except Exception` 吞掉              | ENG-23 | 立即独立 1 行修 |
| P1  | 0.4a   | 用 `sandbox.format_tool_result` 替换 utils 劣化版   | ENG-21 | 与 0.4 系列一起 |
| P1  | 0.4d   | `evaluator` 把 score 回填到 `TaskResult`          | ENG-24 | 与 0.4 系列一起 |
| P1  | 0.4f   | `load_benchmark_data` 校验 task_id 唯一           | ENG-26 | 与 0.4 系列一起 |
| P1  | 0.8a   | 截断已记录的 `Message.tool_calls` 到实际执行集合，满足 OpenAI 协议 | ENG-31 | Phase 2 前 |
| P2  | 0.4c-b | `_execute_tool` 异常分类，不再统一 code=-1             | ENG-23 | 并到 2.4 失败分类 |
| P2  | 0.4e   | `ToolCall` 增加 `effective_parameters`          | ENG-25 | 或随 0.4 主体顺手做 |
| P1  | 0.4b   | `/heartbeat` 真正 refresh TTL + heartbeat jitter  | ENG-22 | 本轮覆盖 GUI/VM/Browser 与长任务，随 Phase 2S.4 做 |
| P2  | 0.7a   | `Sandbox.close()` 默认销毁 session                | ENG-27 | 本轮覆盖 GUI/VM/Browser，随 Phase 2S.5 做 |
| P2  | 0.7b   | HTTP 重试 exponential backoff + jitter + 4xx 跳过 | ENG-28 | 本轮覆盖 GUI/VM/Browser，随 Phase 2S 做 |
| P2  | 0.7c   | `_save_result` 原子写（flush+fsync）                | ENG-29 | 合并到 Commit 0.3 |
| P2  | 0.8b   | `ToolCall.from_dict` + `Trajectory` 完整复原      | ENG-32 | 与 Phase 3.2 一起 |
| P2  | 0.8d   | LLM judge 用 JSON/structured output            | ENG-34 | 与 Phase 5 一起 |
| P3  | 0.7d   | `extract_final_answer` 取最后一次匹配                | ENG-30 | 先统计指标影响 |
| P3  | 0.8c   | `sync wrapper` 统一复用单一 event loop / 或删除       | ENG-33 | 评估外部依赖后决定 |


**合并次序建议**：

1. 紧贴 Commit 0.4 做：**0.4c-a → 0.4a → 0.4d → 0.4f**；0.4e 只有在 0.4 主体已经扩展 ToolCall 字段时顺手做，否则延到 P2。
2. Phase 0 收尾：**0.7c 合并到 0.3**，不要单独重复改 `_save_result`。
3. Phase 1 之后、Phase 2 之前：**0.8a**（采用最小改动：截断已记录 tool_calls 到实际执行集合，先不执行所有 tool_calls）。
4. Phase 2 / 2S 期间：**0.4b、0.4c-b、0.7a、0.7b**，与 heartbeat 续租、失败分类、server shutdown、client backoff 一起做；v2.3 已确认覆盖 GUI/VM/Browser，因此 0.4b/0.7a/0.7b 不再延后。
5. Phase 3 / 5：**0.8b、0.8d** 分别并入 resume 与 eval；**0.7d、0.8c** 保持 P3。

---

## 12.0.1 二次评审（2026-05-18 reality check）

按"这条 ENG 在线上**当前是否真的被触发**"做了第二轮审计。结论是 §12.0 表里不少 commit 应当**降级**或**与其他 Phase 合并**，不再都是 P0/P1。下面是逐条结论：

| ENG | 当前是否真踩到 | 触发条件 | 调整后定性 | 建议 commit 时机 |
| --- | --- | --- | --- | --- |
| ENG-21 | ⚠️ 部分 | utils 劣化版命中 `if "data" in result` 分支时已经只取 `data.result`，与 sandbox 版**效果接近**；但 RAG/web 走 `data.context`/`data.result` 之外的字段时会带噪音 | 维护性问题，非紧急 | **保留 P1**，与 0.4 系列一起做 |
| ENG-22 | ⚠️ 长任务/GUI 场景可能踩 | client 心跳关掉与开启效果等价；目前线上靠 tool call 每次完成时 `refresh_session` 维持 TTL，但两次 tool call 间隔过长或单次 tool call 超过 TTL 时仍可能被 cleanup 误杀 | **升级为 P1**，作为 tool call 刷新之外的 session lease 兜底 | 随 Phase 2S.4 做：server heartbeat 刷 TTL + client heartbeat jitter；§13.4 调大 `session_ttl` 仍保留为第二层兜底 |
| ENG-23 (BdbQuit) | ✅ 是 | 任何 `import pdb; pdb.set_trace()` 然后 `q` 退出都会被 `except Exception` 吞掉 | **升级为 P0 独立 1 行修** | 立即修（不依赖其他 commit） |
| ENG-23 (异常分类) | ❌ 否 | 顺序模式失败率低，分类的统计价值小；并发后才有 ROI | **降级为 P2**，合并到 Commit 2.4 | 与 Phase 2.4 task 失败分类一起 |
| ENG-24 | ✅ 是 | 用户每次跑完都要 join `results_*.jsonl` 和 `evaluation_*.json` 才能看分数 | **保留 P1** | 与 0.4 系列一起做 |
| ENG-25 | ⚠️ 仅 doc/rag 场景 | benchmark item 带 `kwargs`（如 `seed_path`）时才会产生差异；其他场景 `effective == parameters` | **降级为 P2** | 等用户报"传给 sandbox 的参数与 LLM 给的不一致"时再做 |
| ENG-26 | ⚠️ 数据相关 | 公共 benchmark id 通常唯一，自制数据集才可能踩 | **保留 P1**（5 行成本极低） | 与 0.4 系列一起做 |
| ENG-27 | ❌ rollout 模式无影响 | `AgentRunner.stop` 已显式 `destroy_session(resource_types)`；仅 `async with Sandbox()` 自写脚本时漏销毁 | **降级为 P2** | 与 Phase 2S.5 一起 |
| ENG-28 | ❌ 顺序模式无影响 | 并发模式 + server 5xx 才形成 thundering herd | **降级为 P2**，并发改造时再做 | 与 Phase 2 / 2S 一起 |
| ENG-29 | ❌ 顺序模式无影响 | 并发模式 + 多协程同时写 jsonl 才需要锁 | **降级为 P2**，合并到 Commit 0.3 | 与 Commit 0.3 ShutdownManager 一起 |
| ENG-30 | ⚠️ 数据相关 | 模型自我修正多时才会取错；行为 stable 的模型不踩 | **降级为 P3** | 用数据决定是否做（先统计指标变化幅度） |
| ENG-31 | ⚠️ 真协议 bug，当前少触发 | system_prompt 教模型"ONE tool at a time"，绝大多数模型遵守；GPT-4 等偶尔违反就会 break | **保留 P1**，但采用**最小改动**版（截断 `Message.tool_calls` 到 1 个，而不是并发执行多个） | Phase 2 前做 |
| ENG-32 | ❌ 当前完全无调用方 | 全仓 `grep "Trajectory.from_dict"` 无任何调用方；只有 Phase 3.2 resume 才会用 | **降级为 P2**，与 Phase 3 合并 | 与 Phase 3.2 一起 |
| ENG-33 | ❌ rollout 内部不踩 | `SyncAgentRunner` 在 rollout 全仓**无任何内部调用方**（只在 `__init__.py` re-export 给外部）；`pipeline.run()` 走的是顶层 `asyncio.run()` | **降级为 P3 / 或直接删除 SyncAgentRunner** | 评估外部依赖后再定 |
| ENG-34 | ✅ 是 | 任何启用 `evaluation_metric=llm_judgement` 的跑都受影响 | **保留 P2** | 与 Phase 5 一起 |

**调整后真正需要紧贴 Commit 0.4 做的是 4 个固定项 + 1 个条件项**：

1. **ENG-23 (BdbQuit) — 1 行**：立即可做，不依赖任何东西，独立 commit。
2. **0.4a — ~20 行**：format_tool_result 替换。
3. **0.4d — ~25 行**：score 回填。
4. **0.4f — ~20 行**：task_id 去重检查。
5. **0.4e (effective_parameters)** 是条件项：如果 0.4 主体已经做了 trace_id + ToolCall 字段扩展，0.4e 顺手做；否则延到 P2。

**Commit 0.4b 调整说明（2026-05-19）**：

按最新决策，`/heartbeat` 不再只作为 readonly 探针，而是升级为计划内的 session lease 续租兜底。它不替代 tool call 完成后的 `refresh_session`，而是在 LLM 长思考、long bash、长爬虫、GUI/VM/Browser 等场景里，覆盖“tool call 之间或 tool call 期间 TTL 被 cleanup 误杀”的窗口。§13.4 的 `session_ttl=1800` 仍保留，作为心跳异常或极长任务时的第二层保险。

---

## Commit 0.4a：`fix(rollout): use sandbox.format_tool_result instead of inline json.dumps`

### 动机

ENG-21。`runner.py` 第 15 行已经 `from sandbox import Sandbox, format_tool_result`，但第 294 行**实际调用的是 `utils.format_tool_result_for_message`**，后者把整个 dict（包含 `code/message/meta.trace_id/meta.session_id`）都 `json.dumps` 喂给 LLM，污染上下文、浪费 token，并且让设计意图（`sandbox/result_formatter.py` 的注册式工厂）形同虚设。

### 修改文件


| 文件                                       | 改动                                                                                        |
| ---------------------------------------- | ----------------------------------------------------------------------------------------- |
| `rollout/core/runner.py`                 | 第 294 行 `format_tool_result_for_message(tool_result)` → `format_tool_result(tool_result)` |
| `rollout/core/utils.py`                  | 删除 `format_tool_result_for_message` 函数（或保留为薄 alias 并打 DeprecationWarning）                 |
| `rollout/core/__init__.py`（若有 re-export） | 同步移除                                                                                      |


### 关键代码

```python
# rollout/core/runner.py
- from .utils import (
-     create_openai_client,
-     async_chat_completion,
-     extract_final_answer,
-     convert_tool_schema_to_openai,
-     format_tool_result_for_message,
- )
+ from .utils import (
+     create_openai_client,
+     async_chat_completion,
+     extract_final_answer,
+     convert_tool_schema_to_openai,
+ )

# 在 _run_conversation 内：
- result_text = format_tool_result_for_message(tool_result)
+ try:
+     result_text = format_tool_result(tool_result)
+ except ValueError as e:
+     # format_tool_result 找不到注册 formatter 时会 ValueError
+     # 退回到通用 fallback，保证不阻塞 trajectory
+     log.warning(f"no formatter for tool {tool_name}: {e}")
+     result_text = json.dumps(tool_result.get("data", tool_result), ensure_ascii=False, indent=2)
```

### 风险点

- `format_tool_result` 在没注册 formatter 时会 `raise ValueError`；为兼容自定义/实验性 tool，必须保留一个 fallback 路径。
- 旧 trajectory（跑过的 jsonl）里 tool message 的内容会变（更干净），但**不影响重放**，因为它只是 LLM 上下文渲染。

### 可验证测试

1. 跑一条 `web:search` 任务，trajectory 中 `tool` 角色 message 内容应**只有 search 结果文本**，不再包含 `code/meta/trace_id` 等审计字段。
2. 故意改一个不存在的 tool name，应进入 fallback 分支并产生 WARN 日志，task 仍能跑完（标失败）。
3. token 计数对比：相同任务前后跑两次，`tool_result_tokens` 应下降 30–60%。

---

## Commit 0.4b：`feat(sandbox): heartbeat refresh TTL as session lease fallback`

> **v2.5 状态**：升级为 P1，随 Phase 2S.4 实施。`/heartbeat` 改为真正的 session lease 续租接口：server 收到 worker heartbeat 后刷新该 worker 名下 active sessions 的 TTL；client 默认开心跳并加 jitter。§13.4 的 `server.session_ttl=1800` 仍保留为第二层兜底。

### 动机（修正版，2026-05-18）

ENG-22。原描述以为是"client 没发心跳所以 TTL 过期"，**实际根因不一样**。通过审计 `sandbox/server/routes.py:161-191` 和 `sandbox/server/core/tool_executor.py:306-314`，确认：

- 服务端 `/api/v1/lifecycle/heartbeat` **是 readonly 探针**，只调用 `list_worker_sessions`，**不调用 `refresh_session`**，开/关 client 心跳对 TTL 完全无影响。
- 真正延 TTL 的是 `tool_executor.execute()` 末尾的 `refresh_session`（每次 tool call 完成后）。
- 服务端 `cleanup_task` 每 300s 跑一次 `cleanup_expired`（`app.py:572`），默认 `session_ttl=300s`。

因此真实的过期场景有两个：

1. **两次 tool call 间隔 > 300s**（LLM 长思考、o1/deep-thinking 模式、复杂多步推理）。
2. **单次 tool call 自身 > 300s**（long `bash`、长爬虫、code:exec 跑长任务）。`refresh_session` 只在调用**完成后**触发，期间 cleanup_task 仍可能误杀。

历史版本里 `Sandbox._create_client` 强制 `auto_heartbeat=False` 本身**不是根因 bug**——在 server `/heartbeat` 不刷新 TTL 的前提下，开起来也只是多发 readonly 探针。若已先把 client 改成 `auto_heartbeat=True`，必须尽快跟进本 commit 的 server 端续租，否则容易产生“已经有心跳所以 session 安全”的假安全感。

### 修复策略（A + D 组合）

- **A**：让 server `/heartbeat` 真正名实相符——对该 worker 所有 session 调一次 `refresh_session`。client 端默认开心跳并加 jitter，让 keep-alive 真正起作用，解决"LLM 长思考导致两次 tool call 间隔过 TTL"的场景。
- **D**：把 `DEFAULT_SERVER_CONFIG.session_ttl` 从 `300` 提到 `1800`（30 min），保险兜底，覆盖单次 long tool 场景（≤ 30 min 时无 race；> 30 min 需要叠加 **Commit 0.4b-ext**，见可选章节）。

### 修改文件

| 文件 | 改动 |
| --- | --- |
| `sandbox/server/routes.py` | `heartbeat()` 内部新增：遍历该 worker 的所有 session，每个调一次 `resource_router.refresh_session(worker_id, rt)` |
| `sandbox/sandbox.py` | `DEFAULT_SERVER_CONFIG.server.session_ttl: 300 → 1800` |
| `sandbox/sandbox.py` | `_create_client` 把 `auto_heartbeat=False` 改为读 `SandboxConfig` 默认 `True` |
| `sandbox/sandbox.py` | `SandboxConfig` 新增 `auto_heartbeat: bool = True`、`heartbeat_interval: float = 30.0` |
| `sandbox/client.py` | `_heartbeat_loop` 给 `sleep(interval)` 加 ±20% jitter（与 Phase 2S.4 保持一致） |

### 关键代码

```python
# sandbox/server/routes.py
@app.post(HTTPEndpoints.HEARTBEAT)
async def heartbeat(request: Request):
    start_time = asyncio.get_event_loop().time()
    data = await request.json()
    worker_id = data.get("worker_id")
    if not worker_id:
        return JSONResponse(status_code=400, content=build_error_response(...))

    sessions = await server.resource_router.list_worker_sessions(worker_id)

    # NEW: heartbeat 真正延 TTL（对所有该 worker 的 session）
    refreshed: List[str] = []
    for rt in list(sessions.keys()):
        ok = await server.resource_router.refresh_session(worker_id, rt)
        if ok:
            refreshed.append(rt)

    response = build_success_response(
        data={
            "worker_id": worker_id,
            "active_sessions": list(sessions.keys()),
            "refreshed_sessions": refreshed,    # NEW
            "timestamp": datetime.utcnow().isoformat(),
        },
        tool="session:heartbeat",
        ...
    )
    return JSONResponse(content=response)
```

```python
# sandbox/sandbox.py
DEFAULT_SERVER_CONFIG = {
    "server": {
        "title": "Sandbox HTTP Service",
        "description": "HTTP Service for Sandbox",
-       "session_ttl": 300
+       "session_ttl": 1800,   # 30 min；长 LLM 思考友好；> 30 min 的 tool 需要 0.4b-ext
    },
    ...
}


@dataclass
class SandboxConfig:
    ...
    auto_heartbeat: bool = True       # NEW
    heartbeat_interval: float = 30.0  # NEW


def _create_client(self):
    client_config = HTTPClientConfig(
        base_url=self._config.server_url,
        timeout=self._config.timeout,
        max_retries=self._config.retry_count,
        worker_id=self._config.worker_id,
-       auto_heartbeat=False,
+       auto_heartbeat=self._config.auto_heartbeat,
+       heartbeat_interval=self._config.heartbeat_interval,
    )
    self._client = HTTPServiceClient(config=client_config)
```

```python
# sandbox/client.py
import random

async def _heartbeat_loop(self):
    while not self._closed:
        try:
-           await asyncio.sleep(self.config.heartbeat_interval)
+           # ±20% jitter，避免 100 worker 齐步打心跳
+           jitter = random.uniform(0.8, 1.2)
+           await asyncio.sleep(self.config.heartbeat_interval * jitter)
            await self._send_heartbeat()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")
```

### 风险点

- **行为变更**：原 heartbeat 是 readonly，改完后会刷新 TTL。commit message 必须显式说明 `/heartbeat` 从状态探针升级为 session lease keep-alive。
- **可观测性副作用**：100 worker × 30s × N(active session) 次 `refresh_session` 调用会增加 server 内部日志量。`refresh_session` 内部已有 `logger.info`，必要时降级到 DEBUG 或加节流。
- **session_ttl 提到 1800s 的影响**：如果某 task crash 没正常 destroy_session，资源会被持有 30 min（VM/Browser 比较关键）。配合 Commit 0.7a（`Sandbox.close()` 默认销毁 session）+ Phase 2S.5（server SIGTERM 时清 session）一起做就没问题。

### 可验证测试

1. **heartbeat 真延 TTL**：启 server `session_ttl=60`，跑一个 task 在两次 tool call 之间手动 `sleep(90)`，但启用心跳（`heartbeat_interval=20`）。第二次 tool call 应**成功**（不到 60s 内被刷过两次心跳）。同时观察 server log 有 `Session refreshed` 行。
2. **关闭心跳能复现旧症状**：同样 server，显式 `SandboxConfig(auto_heartbeat=False)` 后跑同样 task，第二次 tool call 应**失败**（session not found）。
3. **jitter 验证**：100 worker × 心跳 30s 跑 5 分钟，统计 `/heartbeat` 在 server 端的到达时间直方图，应在 24-36s 窗口内近似均匀分布，无明显 30s 周期尖峰。
4. **默认 TTL=1800**：新建 server 不传 config，`GET /api/v1/session/list` 返回中某 session `expires_at - created_at ≈ 1800s`。

### 可选：Commit 0.4b-ext（覆盖单次 tool > 30 min 场景）

当 `tool_executor.execute` 中 await 一个长 tool 时，开 background `refresh_task`：

```python
# sandbox/server/core/tool_executor.py（仅 sketch）
async def _periodic_refresh(self, worker_id, resource_type, stop_event):
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._refresh_interval)
            return
        except asyncio.TimeoutError:
            await self._resource_router.refresh_session(worker_id, resource_type)

# 在 execute 内：
if resource_type and session_info:
    stop_event = asyncio.Event()
    refresh_task = asyncio.create_task(
        self._periodic_refresh(worker_id, resource_type, stop_event)
    )
    try:
        result = await tool_coro
    finally:
        stop_event.set()
        await refresh_task
```

仅在确实需要 > 30 min 单 tool 时再做；常规长思考/长等待场景先由 0.4b heartbeat 续租 + `session_ttl=1800` 覆盖。

---

## Commit 0.4c：`fix(rollout): re-raise BdbQuit now; classify tool errors later`

> **v2.2 状态**：拆成两步。`0.4c-a` 是 P0，只修 `bdb.BdbQuit` 被吞的问题；`0.4c-b` 是 P2，完整异常分类并入 Commit 2.4 的失败统计。

### 动机

ENG-23。`_execute_tool` 用一个 `except Exception` 把所有异常压扁成 `{"code": -1, "message": str(e), "data": None}`：

- `httpx.TimeoutException` / `httpx.ConnectError` / `HTTPClientError(status_code=4xx 或 5xx)` / `SandboxConnectionError` 全部长一样
- `bdb.BdbQuit` 没被显式 `raise`，pdb 中断会被吞掉
- `ToolCall.success` 只看 `code == 0`，导致 trajectory 无法做"按 root-cause 类别统计失败"的事

### 0.4c-a 修改文件（P0）


| 文件                       | 改动                                                                   |
| ------------------------ | -------------------------------------------------------------------- |
| `rollout/core/runner.py` | 在 `_execute_tool` 的 `except Exception` 之前显式 `except bdb.BdbQuit: raise` |


### 0.4c-a 关键代码（当前批次只做这个）

```python
# rollout/core/runner.py
import bdb

try:
    result = await self.sandbox.execute(tool_name, parameters)
    return result
except bdb.BdbQuit:
    raise
except Exception as e:
    print(f"    ❌ Tool execution error: {e}")
    return {"code": -1, "message": str(e), "data": None}
```

### 0.4c-b 关键代码草案（P2，合并到 2.4）

```python
# rollout/core/runner.py
import bdb
import httpx
from sandbox.client import HTTPClientError
from sandbox.sandbox import SandboxConnectionError

# 错误编码约定（避开 server 用的 0/正数业务码）：
#   -1   非分类的客户端异常（兜底）
#   -10  task/tool 被取消（CancelledError）
#   -11  rollout 端超时（asyncio.wait_for）
#   -20  httpx 连接错误（DNS/TCP）
#   -21  httpx 读写超时
#   -22  server 4xx
#   -23  server 5xx
#   -30  sandbox 连接错（client 未连上）

async def _execute_tool(self, tool_name, parameters, *, trace_id=None, **kwargs):
    if not self.sandbox:
        raise RuntimeError("Sandbox not initialized")
    if kwargs:
        parameters = {**parameters, **kwargs}

    try:
        return await self.sandbox.execute(tool_name, parameters, trace_id=trace_id)
    except bdb.BdbQuit:
        raise
    except asyncio.CancelledError:
        log.warning(f"tool cancelled: {tool_name}")
        raise
    except httpx.TimeoutException as e:
        return {"code": -21, "message": f"tool timeout: {e}", "error_kind": "timeout", "data": None, "meta": {}}
    except httpx.ConnectError as e:
        return {"code": -20, "message": f"connect error: {e}", "error_kind": "connect", "data": None, "meta": {}}
    except HTTPClientError as e:
        status = getattr(e, "status_code", None) or 0
        code = -22 if 400 <= status < 500 else (-23 if 500 <= status < 600 else -1)
        kind = "client_error" if 400 <= status < 500 else "server_error"
        return {"code": code, "message": str(e), "error_kind": kind, "status_code": status, "data": None, "meta": {}}
    except SandboxConnectionError as e:
        return {"code": -30, "message": str(e), "error_kind": "sandbox_disconnect", "data": None, "meta": {}}
    except Exception as e:
        log.exception(f"unexpected tool error: {tool_name}")
        return {"code": -1, "message": str(e), "error_kind": "unknown", "data": None, "meta": {}}
```

0.4c-b 记录 ToolCall 时同步带上 `error_kind`：

```python
tc = ToolCall(
    ...
    code=code,
    error_kind=raw.get("error_kind") if isinstance(raw, dict) else None,
    ...
)
```

### 风险点

- 0.4c-a 是 1 行行为修复：pdb 退出不再被吞，风险很低。
- 0.4c-b 增加对 `httpx`、`HTTPClientError`、`SandboxConnectionError` 的直接 import，要确保 import 路径稳定。可以把异常分类逻辑抽到 `rollout/core/error_kinds.py` 单文件，方便 Phase 2 的 `task_fail` 字段也复用。
- 旧 trajectory 没有 `error_kind`，分析脚本要做向后兼容（缺失时按 "unknown" 处理）。

### 可验证测试

1. 0.4c-a：在 pdb 里手动 `q` 退出，rollout 进程能正常退出（不再吞 BdbQuit）。
2. 0.4c-b：显式让 sandbox server 返回 503，跑 1 个 tool call，trajectory 里 `ToolCall.code=-23, error_kind="server_error", status_code=503`。
3. 0.4c-b：`kill -9` server 后跑 1 个 tool call，应得到 `error_kind="connect"`。
4. 0.4c-b：在 sandbox `WebSearchAPI` 里 `await asyncio.sleep(300)` 模拟挂死，配合 `tool_default_timeout=10`，得到 `error_kind="timeout"`。

---

## Commit 0.4d：`feat(rollout): write back evaluator score to TaskResult`

### 动机

ENG-24。`TaskResult.score: Optional[float]` 已经定义但**全链路从不填**：

- `AgentRunner.run_task` 创建 `TaskResult` 时不传 `score=`（line 191、212）
- `Evaluator.evaluate` 把分数写到独立的 `EvaluationResult` 列表，返回 dict 里也只有 `evaluations`，**不修改原 `TaskResult.score`**

后果：内存里的 `TaskResult.score` 永远是 `None`，`results_*.jsonl` 也没有稳定的 score 伴生索引；要做"按分数筛 trajectory / 排序 / resume"必须临时 join `results_*.jsonl` 和 `evaluation_*.json` 两个结构不同的文件，徒增脚本复杂度。

### 修改文件


| 文件                          | 改动                                                                 |
| --------------------------- | ------------------------------------------------------------------ |
| `rollout/core/evaluator.py` | `evaluate` 内部在生成 `EvaluationResult` 后，同步把 `score` 写回原 `TaskResult` |
| `rollout/pipeline.py`       | 评测完成后写伴生文件 `results_*.scores.jsonl`，不 rewrite 主 `results_*.jsonl` |


### 关键代码

```python
# rollout/core/evaluator.py: evaluate()
for result in results:
    ...
    if not result.success:
        eval_result = EvaluationResult(..., score=0.0, ...)
        result.score = 0.0          # 回填
    elif result.ground_truth is None:
        eval_result = EvaluationResult(..., score=0.0, ...)
        result.score = None         # 显式标记"无 GT"，与 success=False 区分
    else:
        score, details = self._evaluate_single(...)
        eval_result = EvaluationResult(..., score=score, ...)
        result.score = score        # 回填
        scores.append(score)
    evaluations.append(eval_result)
```

```python
# rollout/pipeline.py: run_async() 评测完成后
if evaluation and self.config.save_results:
    # 主 results 文件保持 append-only；score 写伴生文件，避免破坏 Phase 3 resume 语义
    scores_file = self.results_file.replace(".jsonl", ".scores.jsonl")
    with open(scores_file, "w", encoding="utf-8") as f:
        for r in self.results:
            f.write(json.dumps({
                "task_id": r.task_id,
                "success": r.success,
                "score": r.score,
            }, ensure_ascii=False) + "\n")
    print(f"   Scores written: {scores_file}")
```

### 风险点

- 主 `results_*.jsonl` 必须保持 append-only，避免和 Phase 3.1（fcntl flock + append 续推）冲突。
- 下游如果需要单文件自包含结果，可以在离线分析阶段 join `results_*.jsonl` 和 `results_*.scores.jsonl`；不要在 rollout 运行尾声 rewrite 主文件。

### 可验证测试

1. 跑 5 题，`results_*.scores.jsonl` 应有 5 行，且每行包含 `task_id/success/score`。
2. 跑一题 `success=False`，scores 伴生文件里 `.score == 0.0`。
3. 跑一题 `answer=null`（无 ground truth），`TaskResult.score == null` 且 evaluation details 里有 `note == "No ground truth available"`。
4. 主 `results_*.jsonl` 的 mtime/行数不因评测阶段 rewrite 发生变化。

---

## Commit 0.4e：`feat(rollout): record effective_parameters in ToolCall`

### 动机

ENG-25。`_execute_tool` 内部：

```python
parameters = {**parameters, **kwargs}   # 注入 seed_path 等 task_kwargs
result = await self.sandbox.execute(tool_name, parameters)
```

但记录到 `ToolCall.parameters` 的是**合并前的** `tool_args`。如果想审计"为什么传给 sandbox 的 path 不是 LLM 给的"，trajectory 无答案。

### 修改文件


| 文件                       | 改动                                                                         |
| ------------------------ | -------------------------------------------------------------------------- |
| `rollout/core/models.py` | `ToolCall` 新增 `effective_parameters: Optional[Dict[str, Any]] = None`      |
| `rollout/core/runner.py` | `_execute_tool` 改为返回 `(result, effective_params)`；`_run_conversation` 记录两份 |


### 关键代码

```python
# rollout/core/runner.py
async def _execute_tool(self, tool_name, parameters, *, trace_id=None, **kwargs):
    effective = {**(parameters or {}), **kwargs}
    try:
        result = await self.sandbox.execute(tool_name, effective, trace_id=trace_id)
    except ... as e:
        # 同 0.4c 的分类
        result = {"code": ..., ...}
    return result, effective


# _run_conversation 内：
raw, effective_params = await self._execute_tool(tool_name, tool_args, trace_id=trace_id, **task_kwargs)
tc = ToolCall(
    tool_name=tool_name,
    parameters=tool_args,                 # LLM 原始 args
    effective_parameters=effective_params, # 实际发到 sandbox 的
    ...
)
```

### 风险点

- 改 `_execute_tool` 返回签名是 breaking change，但调用方只有 `_run_conversation` 一处，影响面可控。
- `effective_parameters` 体积可能比 `parameters` 大不少（如带 `seed_path` 时可能附带额外配置），注意 jsonl 行长度。

### 可验证测试

1. 在 benchmark item 的 `kwargs` 里加一个特殊 key（如 `_audit_tag: "abc"`），跑一题，`ToolCall.effective_parameters._audit_tag == "abc"`，而 `ToolCall.parameters` 里没有。
2. 100 题跑完，`jq '[.trajectory.tool_calls[].effective_parameters | keys] | flatten | unique' results.jsonl` 能列出所有 effective key。

---

## Commit 0.4f：`feat(rollout): warn/abort on duplicate task_id in benchmark data`

### 动机

ENG-26。`load_benchmark_data` 完全不校验 task_id 唯一性。下游全链路用 task_id 作为 join key（results / evaluation / resume / checkpoint），重复 id 会 silent overwrite，造成"评测分数和 jsonl 行数不一致"。

### 修改文件


| 文件                       | 改动                                                                      |
| ------------------------ | ----------------------------------------------------------------------- |
| `rollout/pipeline.py`    | `load_benchmark` 末尾加 dedup check                                        |
| `rollout/core/config.py` | 新增 `on_duplicate_task_id: Literal["warn", "error", "ignore"] = "error"` |


### 关键代码

```python
# rollout/pipeline.py: load_benchmark()
items = [BenchmarkItem.from_dict(item) for item in raw_data]

ids = [it.id for it in items]
seen = set()
duplicates = []
for i in ids:
    if i in seen:
        duplicates.append(i)
    seen.add(i)

if duplicates:
    mode = self.config.on_duplicate_task_id
    msg = f"Found {len(duplicates)} duplicate task_id(s): {duplicates[:10]}"
    if mode == "error":
        raise ValueError(msg)
    elif mode == "warn":
        print(f"⚠️ {msg}")
    # ignore: 什么都不做
```

### 风险点

- 已有数据集（自己生成的 jsonl）可能因为历史原因带重复 id，必须用 `on_duplicate_task_id="warn"` 显式开放，否则升级即破坏。
- 默认值取 `"error"` 是激进选择，理由是"数据集 schema 错误应当 fail-fast"；如果团队偏好稳态，可改成 `"warn"`。

### 可验证测试

1. 准备一份故意重复 id 的 jsonl，跑 pipeline，默认应抛 `ValueError` 阻止运行。
2. 显式配 `on_duplicate_task_id="warn"`，应只打印 warn 但继续运行；产出的 jsonl 行数 = 输入行数（包括重复）。

---

## Commit 0.7a：`fix(sandbox): destroy sessions on Sandbox.close by default`

### 动机

ENG-27。`HTTPServiceClient.close(destroy_sessions=False)` 默认不销毁，`Sandbox.close()` 第 866-868 行调用 `await self._client.close()` 没传参数，默认就是 False。用户写 `async with Sandbox()` 退出后，server 端 session 还 hang 到 TTL 才被回收。

`AgentRunner.stop` 单独显式 `destroy_session(self.config.resource_types)` 把它盖住了，但**只销毁 rollout 配置里声明的 resource_types**——自动创建的 session（通过 `vm:screenshot` prefix 自动 create）会漏。

### 修改文件


| 文件                   | 改动                                                          |
| -------------------- | ----------------------------------------------------------- |
| `sandbox/sandbox.py` | `Sandbox.close` 增加 `destroy_sessions=True` 默认参数并透传          |
| `sandbox/sandbox.py` | `SandboxConfig` 新增 `destroy_sessions_on_close: bool = True` |


### 关键代码

```python
# sandbox/sandbox.py
async def close(self, destroy_sessions: Optional[bool] = None):
    if not self._connected:
        return

    if destroy_sessions is None:
        destroy_sessions = getattr(self._config, "destroy_sessions_on_close", True)

    if self._client:
        await self._client.close(destroy_sessions=destroy_sessions)
        self._client = None

    self._connected = False
    self._started = False
    logger.info(f"👋 Sandbox closed (destroy_sessions={destroy_sessions}, worker_id: {self.worker_id})")
```

### 风险点

- `client.close(destroy_sessions=True)` 通过 `POST /api/v1/worker/disconnect` 通知 server。若 server 不可达（已挂），需要 try/except 包裹，否则 close 本身抛异常。`client.py` 第 219-226 行已经 try/except 了。
- 主动 destroy 后再有 in-flight tool call 会 404，建议先 cancel 所有 pending 再调 close（由 Phase 0.3 的 ShutdownManager 保证顺序）。

### 可验证测试

1. 跑 1 个一次性脚本 `async with Sandbox() as s: await s.execute("web:search", {...})`，退出后 `curl /api/v1/session/list` 应为空。
2. 显式 `await sandbox.close(destroy_sessions=False)`，session 应仍在直到 TTL。

---

## Commit 0.7b：`feat(sandbox): exponential backoff with jitter + 4xx no-retry`

### 动机

ENG-28。`_request` retry 策略是 `await asyncio.sleep(retry_delay * (attempt + 1))`：

- 线性递增（1s/2s/3s），不是 exponential
- 没有 jitter，100 worker 同一秒撞 5xx 后会同步重试
- 不区分 4xx/5xx；当前因为 `HTTPClientError` 不继承 `httpx.HTTPError`，4xx 实际上**侥幸**不会重试，但这是"隐式协议"

### 修改文件


| 文件                  | 改动                                                                               |
| ------------------- | -------------------------------------------------------------------------------- |
| `sandbox/client.py` | `_request` 改用 exponential backoff + jitter + 显式区分异常                              |
| `sandbox/client.py` | `HTTPClientConfig` 新增 `retry_backoff: float = 2.0` 和 `retry_jitter: float = 0.3` |


### 关键代码

```python
# sandbox/client.py
import random

async def _request(self, method, endpoint, data=None, timeout=None):
    if self._client is None:
        raise RuntimeError("Client not connected. Call connect() first.")

    request_timeout = timeout or self.config.timeout

    for attempt in range(self.config.max_retries):
        try:
            if method.upper() == "GET":
                response = await self._client.get(endpoint, timeout=request_timeout)
            else:
                response = await self._client.post(endpoint, json=data, timeout=request_timeout)

            result = response.json()

            if response.status_code >= 400:
                error_msg = result.get("message") or result.get("error") or str(result)
                err = HTTPClientError(
                    f"Request failed: {error_msg}",
                    status_code=response.status_code,
                    response=result,
                )
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    # 4xx（除 429）不可重试
                    raise err
                # 5xx 或 429 → 进入 backoff
                last_err = err
            else:
                return result

        except httpx.TimeoutException as e:
            last_err = HTTPClientError(f"Request timed out: {e}")
        except httpx.HTTPError as e:
            last_err = HTTPClientError(f"HTTP error: {e}")

        if attempt == self.config.max_retries - 1:
            raise last_err

        base = self.config.retry_delay * (self.config.retry_backoff ** attempt)
        jitter = 1.0 + random.uniform(-self.config.retry_jitter, self.config.retry_jitter)
        await asyncio.sleep(base * jitter)

    raise HTTPClientError("Request failed after all retries")
```

### 风险点

- 4xx 不重试是行为变更（之前因为继承关系巧合也是不重试，但 429 之前**会**重试，是 OK 的）。要确认所有调用方对 4xx 立即抛异常的行为有预期；最安全做法是先打 `WARN` 日志一周，确认无回归再切默认。
- `retry_backoff=2.0` 在 `max_retries=3` 时总等待 = 1 + 2 + 4 = 7s，比线性的 1+2+3=6s 略长，但对 5xx 风暴更友好。

### 可验证测试

1. 让 server 返回 `400`，client 应**只调用 1 次**就抛异常（之前会 3 次）。
2. 让 server 前两次返回 `503` 第三次返回 200，client 应成功并耗时 ≥ 3s 且 ≤ 7s（exponential + jitter 范围）。
3. 100 worker 并发遇 503，retry 时间分布应在 `[0.7, 1.3] / [1.4, 2.6] / [2.8, 5.2]` 区间内均匀分布。

---

## Commit 0.7c：`fix(rollout): atomic _save_result with flush+fsync`

### 动机

ENG-29。`pipeline._save_result` 当前实现：

```python
with open(self.results_file, 'a', encoding='utf-8') as f:
    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
```

- 无 `flush() + os.fsync()`，进程 SIGKILL 时缓冲区可能丢
- 多协程并发模式下没有锁
- 与 Phase 0.3 的目标重叠但 0.3 主要做 ShutdownManager + cancel-safe，这里专做"写入原子性"

### 修改文件


| 文件                    | 改动                                                              |
| --------------------- | --------------------------------------------------------------- |
| `rollout/pipeline.py` | `_save_result` 改写为 lock + write + flush + fsync                 |
| `rollout/pipeline.py` | `RolloutPipeline.__init__` 加 `self._save_lock = asyncio.Lock()` |


### 关键代码

```python
# rollout/pipeline.py
def __init__(self, ...):
    ...
    self._save_lock = asyncio.Lock()
    self._results_fd = None  # 长开 fd，避免每次 open

async def _save_result_async(self, result: TaskResult) -> None:
    payload = ... # 同原逻辑
    line = json.dumps(payload, ensure_ascii=False) + "\n"

    async with self._save_lock:
        # 把 blocking IO 丢到默认 executor，不阻塞 event loop
        await asyncio.to_thread(self._sync_append_and_fsync, line)

def _sync_append_and_fsync(self, line: str) -> None:
    with open(self.results_file, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
```

### 风险点

- `os.fsync` 每次都 flush 到磁盘，5000 题写入会比无 fsync 多约 5–15s（机械盘可能更多）；SSD 影响很小。可以提供 `save_fsync: bool = True` 让大规模批跑选择关闭。
- 0.3 commit 已经覆盖一部分原子写入；建议两个 commit 合并 review，避免冲突。

### 可验证测试

1. 跑 100 题，每隔 10 题 `kill -9` rollout 进程，再用 `wc -l results_*.jsonl`，行数应严格等于成功完成的任务数（不会有半行）。
2. 并发 64，跑 5000 题，jsonl 应可用 `jq -c .` 全行 parse 成功。

---

## Commit 0.7d：`fix(rollout): use last-match in extract_final_answer`

### 动机

ENG-30。`extract_final_answer` 用 `re.search` 取第一个匹配。模型输出 `"the answer is 42… actually, looking again, the final answer is 43"` 会被解析成 **42**，evaluator 因此给低分，影响指标。

### 修改文件


| 文件                      | 改动                                             |
| ----------------------- | ---------------------------------------------- |
| `rollout/core/utils.py` | `extract_final_answer` 改用 `re.findall` 取最后一个匹配 |


### 关键代码

```python
def extract_final_answer(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"(?:final answer|answer is|the answer is|answer:)\s*[:\-]?\s*(.+?)(?:\n|$)",
        r"\*\*Answer\*\*:?\s*(.+?)(?:\n|$)",
        r"(?:therefore|thus|so|hence),?\s+(?:the answer is\s+)?(.+?)(?:\.|$)",
    ]

    for pattern in patterns:
-       match = re.search(pattern, text, re.IGNORECASE)
-       if match:
-           answer = match.group(1).strip()
+       matches = re.findall(pattern, text, re.IGNORECASE)
+       if matches:
+           answer = matches[-1].strip()  # 取最后一次匹配
            answer = re.sub(r'\s*\.$', '', answer)
            return answer

    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if lines:
        return lines[-1]
    return text.strip()
```

### 风险点

- 历史 trajectory 重新跑 evaluator 时，predicted_answer 会变；如果以历史 score 为 baseline 做差分，记得明示这一变动。
- 极端 case：模型连续说 "the answer is X" N 次但其实是举反例，取最后一次也未必对；但比"取第一次"统计上更合理。

### 可验证测试

1. 单元测试：`extract_final_answer("the answer is 42. Wait, actually the answer is 43.") == "43"`。
2. 在一批 5–10 个已知正确答案的 task 上做 A/B：predicted_answer 变化的比例应 <5%，且差异方向偏向"修正错误"而非引入新错误。

---

## Commit 0.8a：`fix(rollout): keep assistant tool_calls paired with executed tool messages`

> **v2.2 状态**：采用最小改动版。当前仍只执行 1 个 tool call，但必须把 assistant message 里记录的 `tool_calls` 同步截断到实际执行集合，保证 OpenAI tool protocol 配对正确。执行所有 tool calls 留给后续增强。

### 动机

ENG-31。`runner.py` 第 271 行：

```python
for tool_call in assistant_message.tool_calls[:1]:  # Execute one at a time
```

但前一行：

```python
tool_calls=[tc.model_dump() for tc in assistant_message.tool_calls] if assistant_message.tool_calls else None
```

把全部 N 个 tool_calls 都写进了 `Message.tool_calls`。下一轮把 messages 喂回 LLM 时，OpenAI 协议要求**每个 tool_call_id 都有对应的 `role="tool"` 消息**——N≥2 会被 OpenAI API 拒（`tool_call_id` 找不到对应 tool message）或导致模型混乱。

这是个"看起来在工作"的隐性 bug：当模型总是只生成 1 个 tool call 时观察不到；只要切到一个能并行 tool call 的 prompt/模型，就会立即爆。

### 修改文件


| 文件                       | 改动                                               |
| ------------------------ | ------------------------------------------------ |
| `rollout/core/runner.py` | 继续只执行第 1 个 tool call；同步把 `msg.tool_calls` 截断为同一个 tool call |
| `rollout/core/config.py` | 暂不新增配置；后续若要执行多个 tool call，再引入 `max_tool_calls_per_turn` |


### 关键代码

```python
if assistant_message.tool_calls:
    tool_calls = list(assistant_message.tool_calls)
    executed_tool_calls = tool_calls[:1]

    if len(tool_calls) > 1:
        log.warning(
            f"Model returned {len(tool_calls)} tool_calls; executing 1 and truncating recorded tool_calls"
        )

    # 关键：assistant message 中记录的 tool_calls 必须与后续 tool message 一一配对
    msg.tool_calls = [tc.model_dump() for tc in executed_tool_calls]

    for tool_call in executed_tool_calls:
        tool_name = tool_call.function.name
        ...
        raw, effective_params = await self._execute_tool(tool_name, tool_args, ...)
        tc = ToolCall(...)
        trajectory.tool_calls.append(tc)

        result_text = format_tool_result(raw)

        tool_msg = Message(
            role="tool",
            content=result_text,
            tool_call_id=tool_call.id,
            name=tool_name,
        )
        messages.append(tool_msg)
        trajectory.messages.append(tool_msg)
```

### 风险点

- 这是协议正确性修复，不改变"一次只执行一个 tool call"的行为模型。
- 如果未来要执行多个 tool call，必须重新评估 trajectory 顺序、server 端 session 串行锁，以及 `max_tool_calls_per_turn` 的默认值。
- 截断会丢弃模型同轮返回的其他 tool call；这与当前实际执行行为一致，只是把记录改成真实发生的事情。

### 可验证测试

1. 构造 prompt 强迫模型一次返回 2 个 tool_calls，运行后 `Message.tool_calls`（assistant）应只保留实际执行的 1 个 id，并与后续 `role=tool` message 的 `tool_call_id` 完全一致。
2. 喂回 OpenAI API（用 `messages` 拼到下一轮）应不报 `Invalid tool_call_id` 错。
3. 验证 task 级行为与旧版一致：仍只执行第 1 个 tool call，不新增同轮多工具执行。

---

## Commit 0.8b：`feat(rollout): ToolCall.from_dict + Trajectory full restore`

### 动机

ENG-32。`Trajectory.from_dict` 第 139 行：

```python
tool_calls=[],  # Not reconstructing tool calls from dict
```

写出去的 jsonl 完整，但反序列化丢弃 tool_calls。直接影响：

- Phase 3.2 resume：续推时无法判断"上次跑到第几个 tool call"
- Phase 5.1 evaluator：online_env 模式想基于 tool 调用顺序做评分时无信息
- 离线 audit：无法跑"对所有跑过的 task 重新计算 tool execution stats"

### 修改文件


| 文件                       | 改动                                                |
| ------------------------ | ------------------------------------------------- |
| `rollout/core/models.py` | 新增 `ToolCall.from_dict`；修正 `Trajectory.from_dict` |


### 关键代码

```python
@dataclass
class ToolCall:
    ...

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolCall':
        return cls(
            tool_name=data.get("tool_name", ""),
            parameters=data.get("parameters", {}) or {},
            effective_parameters=data.get("effective_parameters"),
            formatted_result=data.get("formatted_result", ""),
            result=data.get("result"),
            success=bool(data.get("success", True)),
            code=data.get("code"),
            message=data.get("message", ""),
            error=data.get("error"),
            error_kind=data.get("error_kind"),
            resource_type=data.get("resource_type"),
            session_id=data.get("session_id"),
            trace_id=data.get("trace_id"),
            execution_time_ms=float(data.get("execution_time_ms", 0.0)),
        )


# Trajectory.from_dict:
tool_calls=[ToolCall.from_dict(tc) for tc in data.get("tool_calls", [])],
```

### 风险点

- 旧 jsonl 没有所有新字段（如 `effective_parameters`、`error_kind`），`from_dict` 必须全部带 `data.get(..., default)`，否则 KeyError。
- `result` 字段是 `Any`，反序列化不会做类型恢复（保持 dict/list）；下游消费方需要意识到。

### 可验证测试

1. 跑 5 题 → 拿到 jsonl → 用 `Trajectory.from_dict` 逐条 parse → `len(traj.tool_calls)` 应等于原 trajectory；spot-check 一个 `trace_id` 字段。
2. 用 0.4 之前的 jsonl（无新字段）做 parse，应不抛异常，所有新字段默认值齐全。

---

## Commit 0.8c：`refactor(rollout|sandbox): reuse single event loop in sync wrappers`

### 动机

ENG-33。两个 sync wrapper 模式：

```python
# rollout/core/runner.py SyncAgentRunner
def _run_async(self, coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# sandbox/sandbox.py Sandbox._run_async
def _run_async(self, coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
```

每次调用新建/销毁 loop。`httpx.AsyncClient` 绑定 loop，跨 loop 复用必报 `RuntimeError: Event loop is closed`。**当前仅靠"调用方约定不混用 sync/async"**绕过，是个埋雷。

### 修改文件


| 文件                       | 改动                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `rollout/core/runner.py` | `SyncAgentRunner` 持有 long-lived loop + `asyncio.run_coroutine_threadsafe`，或直接禁用并打 DeprecationWarning（**推荐**） |
| `sandbox/sandbox.py`     | 同上策略，且把所有 `*_sync` 方法标 deprecated                                                                            |


### 关键代码（推荐：弃用 sync wrapper）

```python
# rollout/core/runner.py
import warnings

class SyncAgentRunner:
    def __init__(self, config, worker_id=None):
        warnings.warn(
            "SyncAgentRunner is deprecated and will be removed in v3.0. "
            "Use AgentRunner with asyncio.run() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._runner = AgentRunner(config, worker_id)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_async(self, coro):
        loop = self._ensure_loop()
        return loop.run_until_complete(coro)

    def close(self):
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
```

### 风险点

- 真正"复用 loop"会让 httpx client 横跨多次调用 OK；但 `run_until_complete` 内 await 的协程不能再嵌套 `run_until_complete`，否则 `RuntimeError: This event loop is already running`。
- 团队是否还需要 sync 路径？如果不需要，**直接删掉是最干净的**——只保留 async API + 一条 `asyncio.run(...)` 顶层封装。

### 可验证测试

1. 连续调用 `runner.run_task(t1); runner.run_task(t2); runner.run_task(t3)`（同 SyncAgentRunner 实例），应都成功而不报 `Event loop is closed`。
2. 走 deprecation 路径：`pytest --filterwarnings error::DeprecationWarning` 应能 catch 到。

---

## Commit 0.8d：`refactor(eval): structured-output + json mode for llm_judgement`

### 动机

ENG-34。`_evaluate_llm_judgement` 评分解析逻辑：

```python
if "1" in content and "0" not in content.split("1")[0]:
    if content.find("1") < content.find("0") or content.find("0") == -1:
        score = 1.0
elif "0" in content:
    score = 0.0
```

- LLM 回复 `"Score: 10/10, mostly correct"` → "1" 在前，"0" 也在前，判断行为不可预测
- `except:` 是裸 except，吞 `KeyboardInterrupt` / `SystemExit`
- 没有强约束输出格式

### 修改文件


| 文件                          | 改动                                                                                                                                                      |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rollout/core/evaluator.py` | `_evaluate_llm_judgement` 改用 JSON mode（`response_format={"type": "json_object"}`）或 OpenAI structured output；prompt 强制 `{"score": 0|1, "reason": "..."}` |


### 关键代码

```python
def _evaluate_llm_judgement(self, predicted, ground_truth):
    if self._client is None:
        self._client = create_openai_client(api_key=self.api_key, base_url=self.base_url)

    system_content = (
        "You are a strict evaluator. Compare predicted answer to ground truth. "
        "Return JSON object with exactly two keys: "
        '{"score": 0 or 1, "reason": "<short explanation>"}. '
        "Do not include any text outside the JSON object."
    )
    eval_prompt = (
        f"Ground truth: {ground_truth}\n"
        f"Predicted: {predicted}\n"
        f"Is the prediction correct? Output JSON only."
    )

    try:
        response = chat_completion(
            self._client,
            max_retries=self.max_retries,
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": eval_prompt},
            ],
            temperature=self.temperature,
            response_format={"type": "json_object"},
            **self.extra_params,
        )
        content = response.choices[0].message.content.strip()
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            log.warning(f"llm_judgement non-JSON output: {content[:120]}")
            return 0.0, {"error": "non-json output", "raw": content}

        score = 1.0 if int(obj.get("score", 0)) == 1 else 0.0
        if not predicted or predicted.strip() == "":
            score = 0.0
        return score, {"correctness": int(score), "reason": obj.get("reason", ""), "raw": content}

    except json.JSONDecodeError as e:
        return 0.0, {"error": f"json decode: {e}"}
    except Exception as e:
        return 0.0, {"error": str(e)}
```

### 风险点

- 不是所有 LLM 都支持 `response_format`：OpenAI/vLLM 支持，OpenRouter 部分模型支持；fallback 路径要打 WARN。
- 已有评测结果是按旧 prompt 跑的，重跑会有 ±5% 左右差异，使用者要意识到这是个"评分一致性"变更。

### 可验证测试

1. 单元测试：mock OpenAI 返回 `'{"score": 1, "reason": "exact match"}'` → score 应等于 1.0。
2. 喂一个会让旧逻辑误判的回复（`"Score: 10/10, mostly correct"`），新逻辑应基于 JSON 字段判断，不再受字符顺序影响。
3. 强制模型故意输出非 JSON，应进入 fallback，score=0，details.error 非空，无 KeyError。

---

## 12.X 维护规则

- 本章 commit（0.4a 到 0.8d）单独走 review，不与 Phase 0–5 主线 commit 混在同一个 PR 里。
- 每完成一个补丁，在 §0 commit 表对应行加 `[done @ <hash>]`。
- 如审计中再发现同类问题，新增 ENG 编号从 ENG-35 起，并在本章追加 commit 模板。
- 跑完所有 P0+P1 补丁后，**重新生成一次 5 题 smoke test 的 trajectory jsonl**，作为后续 Phase 2 改造的对照基线。

---

# 13. 运行时关键数值清单（"硬编码默认值" 全表）

> 这一章独立于补丁系列，目的是把分散在代码里的"会影响运行的魔数"汇总成一张表，跑前/跑中可以对照。**重要程度**列说明这个值在什么场景下会变成瓶颈或炸点。
>
> 表格规则：
>
> - **状态** ✓ = 已暴露到配置文件（可直接改 JSON）；△ = 暴露但容易遗漏；✗ = 未暴露
> - **影响半径**：global / per-task / per-tool-call

## 13.1 server 端关键值

| 字段 | 当前值 | 代码位置 | 状态 | 影响半径 | 重要度 | 说明 / 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_ttl` | 300s（GUI/RAG configs 是 600s） | `sandbox/sandbox.py:53` (DEFAULT)，各 sandbox-server JSON | ✓ | global | ⭐⭐⭐ | **ENG-22 的第二层兜底**。0.4b 会让 heartbeat 刷 TTL；Web/RAG 长 reasoning 或长 tool 场景仍建议 1800s；GUI 已是 600s；ds/doc 是 300s 偏小 |
| `cleanup_task interval` | 300s 固定 | `sandbox/server/app.py:574`；`config_loader.py` 里有未接通的 `cleanup_interval` | ✗ | global | ⭐⭐ | **与 `session_ttl` 内部联动，不暴露新用户配置**。建议 server 启动时派生：`max(30, min(300, session_ttl // 2))`，并替换硬编码 `sleep(300)`。现有 `cleanup_interval` 是空置字段，0.9 必须删除或显式接入生效，不能继续静默存在 |
| `vlm_timeout` | 300s | `configs/sandbox-server/doc_config.json` | ✓ | per-tool-call | ⭐⭐ | VLM 推理慢的话调大 |
| `batcher_trigger_batch_size` / `batcher_max_batch_size` | 16 / 32 | `sandbox/server/backends/resources/rag.py:622-623`；configs 可覆盖 | ✓ | global (RAG) | ⭐⭐ | 并发 100 时大幅影响 RAG 吞吐；可上调 32/64 |
| `batcher_max_wait_time` | 50ms | `rag.py:1016` (`cfg.get("batcher_max_wait_time", 0.05)`) | △ | global (RAG) | ⭐ | 高并发时减小（如 10ms）减少尾延迟 |
| `batcher check_interval` | 10ms 固定 | `rag.py:625` | ✗ | global (RAG) | ⭐ | 内部轮询，目前没暴露，影响 batch 收集精度 |
| RAG `top_k` 默认 | 5 | `rag.py:460,492,501,719` | △ | per-tool-call | ⭐ | LLM 传入时可覆盖；configs/sandbox-server 也可放默认 |
| RAG `max_length`（embedding 截断） | 512 tokens | `rag.py:224` | ✗ | per-tool-call | ⭐ | 超长 query 会被截掉尾部信息 |
| websearch `max_workers`（共享 ThreadPoolExecutor） | 5（每次新建）| `sandbox/server/backends/tools/websearch.py:386,441` | △ | per-tool-call | ⭐⭐⭐ | **ENG-5**。**每次 `execute()` 调用都 new 一个 5-thread 池**。100 worker 同时调 → 500 线程瞬时；改成共享池且大小可配是 Phase 2S.4 的事 |
| Jina `timeout` | 30s | `websearch.py:282` | △ | per-tool-call | ⭐⭐ | Jina 慢 → 串联抖动整个 task |
| Jina `retry_max_attempts` | 3 | `websearch.py:282` | △ | per-tool-call | ⭐ | exponential backoff 已经在用（`retry_initial_delay=1.0`，2^attempt）|
| ds_tool stdout `MAX_CHARS` | 5000 | `ds_tool.py:386` | ✗ | per-tool-call | ⭐ | 长 stdout 被截 |

## 13.2 client / sandbox facade 端关键值

| 字段 | 当前值 | 代码位置 | 状态 | 影响半径 | 重要度 | 说明 / 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| `HTTPClientConfig.timeout` | 60s | `client.py:69` | ✓ via `sandbox_timeout` | per-tool-call | ⭐⭐⭐ | 长 tool（bash / web visit）需要调大 |
| `HTTPClientConfig.max_retries` | 3 | `client.py:70` | ✓ via `retry_count` | per-tool-call | ⭐⭐ | 失败重试 |
| `HTTPClientConfig.retry_delay` | 1.0s（线性 `delay * (attempt+1)`）| `client.py:71` | ✗ | per-tool-call | ⭐⭐ | **ENG-28**。无 jitter；100 并发遇 5xx 形成 thundering herd |
| `HTTPClientConfig.heartbeat_interval` | 30s | `client.py:73` | ✗ | per-worker | ⭐⭐ | **ENG-22/0.4b**。应小于 `session_ttl`，并在 `_heartbeat_loop` 加 jitter，避免 100 worker 齐步 |
| `HTTPClientConfig.auto_heartbeat` | True（历史版本曾在 `sandbox.py:_create_client` 强制覆盖为 False） | `client.py:72`，`sandbox.py:_create_client` | △ | per-worker | ⭐⭐ | **ENG-22/0.4b**。client 心跳已可开启，但在 server `/heartbeat` 刷 TTL 前只产生探针流量；0.4b 必须补齐 server 端 lease 续租，避免“有心跳但不续命”的假安全感 |
| `SandboxConfig.server_startup_timeout` | 30s | `sandbox.py:106` | ✓ | one-shot | ⭐⭐ | 自启 server 慢时调大；GUI 等重资源会触发 |
| `SandboxConfig.server_check_interval` | 0.5s | `sandbox.py:107` | ✓ | one-shot | ⭐ | server 启动轮询间隔 |
| `Sandbox._check_server_online_async` httpx timeout | 5s | `sandbox.py:944,954` | ✗ | one-shot | ⭐ | server 慢启时可能误判为离线 |
| `Sandbox.close()` `destroy_sessions` | False | `sandbox.py:861` (透传)，`client.py:198` 默认 | △ | per-worker | ⭐⭐ | **ENG-27**。rollout 内部已由 `AgentRunner.stop` 显式销毁，外部用户写 `async with Sandbox()` 时会泄漏 |

## 13.3 rollout 端关键值

| 字段 | 当前值 | 代码位置 | 状态 | 影响半径 | 重要度 | 说明 / 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| `max_turns` | 100 | `rollout/core/config.py:34` | ✓ | per-task | ⭐⭐⭐ | LLM 上下文成本主因；不收敛时会用完 100 turn 才退出 |
| `max_retries`（LLM 调用） | 3 | `config.py:35` | ✓ | per-LLM-call | ⭐⭐ | LLM API 短暂 5xx 时的重试 |
| `max_workers` | 1 | `config.py:36` | ✓ | global | ⭐⭐⭐ | 当前 `_run_parallel` 是 stub 状态，配置 > 1 也不真并发（Phase 2.2 解决）|
| `async_chat_completion retry_wait` | 0.5s | `utils.py:48` | ✗ | per-LLM-call | ⭐ | 函数参数默认值，外部覆盖路径目前没接通 |
| `async_chat_completion retry_backoff` | 2.0 | `utils.py:49` | ✗ | per-LLM-call | ⭐ | 同上 |
| `format_tool_result_for_message max_length` | 4000 chars | `utils.py:193` | ✗ | per-tool-call | ⭐⭐ | 喂给 LLM 的 tool 返回最大长度；长网页 / 长 accessibility tree 会被截掉尾部信息。Phase 2 加并发后 tool 输出更密集，可能要调到 6000-8000 |
| `evaluator_temperature` | 0.0 | `config.py:51` | ✓ | per-eval | ⭐ | LLM judgement 默认 deterministic |
| `evaluator_max_retries` | 3 | `config.py:52` | ✓ | per-eval | ⭐ | 同 LLM 重试 |
| `sandbox_timeout`（rollout 侧） | 120 | `config.py:63` | ✓ | per-tool-call | ⭐⭐⭐ | 透传到 HTTPClient.timeout；长 tool 需要调大（doc/ds 配置已是 300）|
| **三层超时（task / llm / tool）** | **未实现** | — | ✗ | per-task | ⭐⭐⭐⭐ | **ENG-10**。当前无任务级 / LLM 级 / tool 级 timeout；单 task 卡死会把 worker 卡住。Phase 0.5 解决 |

## 13.4 跑前 checklist：必须按场景调的字段

按场景给出**最该改**的字段（其他保持默认）：

### Web / RAG（顺序 → 并发过渡）

- `max_workers`: 1 → 8（先小后大；Phase 2 完成后再上 100）
- `sandbox_timeout`: 120 → 180（web visit 慢页面常 1-2 min）
- `server.session_ttl`: 300 → 1800（**ENG-22/0.4b 的第二层兜底**；第一层是 heartbeat 续租）
- `batcher_max_batch_size` (RAG): 32 → 64（如 GPU 够）
- `format_tool_result_for_message max_length`: **要在 utils.py 改默认值或抽到 config**

### GUI / VM / Browser

- `max_workers`: 1（**资源决定，物理上限 = VM 实例数**；典型 8-16）
- `server.session_ttl`: 600（已是）
- `server_startup_timeout`: 30 → 90（VM 起步慢）
- `sandbox_timeout`: 120 → 300（VM 操作 + 截屏 + accessibility 解析慢）

### Doc / DS（LLM judgement 评测密集）

- `evaluator_max_retries`: 3 → 5
- `vlm_timeout`: 300 → 600（PDF/图片 VLM 推理）
- `ds_tool MAX_CHARS`: 5000 → 20000（**需要把魔数抽出来才能改**）

## 13.5 建议：哪些值最该 "先治理再调"

按 "改一次能影响最多场景" 排序：

1. **`cleanup_task interval`**（`server/app.py:574`）→ 不暴露为用户配置，由 `session_ttl` 派生：`max(30, min(300, session_ttl // 2))`；同时处理 `config_loader.py` 里未生效的 `cleanup_interval`，要么删除，要么显式接入生效，不能做空置功能
2. **`websearch ThreadPoolExecutor max_workers`**（`websearch.py:386,441`）→ 抽到 web tool 配置；改共享池而不是 per-call 新建（**Phase 2S.4**）
3. **`format_tool_result_for_message max_length`**（`utils.py:193`）→ 抽到 `RolloutConfig`；高并发下要给 token 预算上限
4. **`Sandbox._check_server_online_async timeout`**（`sandbox.py:944,954`）→ 抽到 `SandboxConfig`
5. **`async_chat_completion retry_wait / retry_backoff`**（`utils.py:48-49`）→ 抽到 `RolloutConfig.llm_retry_*`（Phase 1 时一起做）
6. **RAG `max_length`（embedding 截断）**（`rag.py:224`）→ 抽到 RAG config
7. **`ds_tool MAX_CHARS`**（`ds_tool.py:386`）→ 抽到 ds tool config

这 7 个建议作为 **Commit 0.9：`feat(config): rationalize runtime magic numbers and derived cleanup interval`** 单独处理，~150 LoC。可以放在 Phase 0 末尾、Phase 1 之前。

### 13.5.1 `cleanup_task interval` 实施口径

`cleanup_task interval` 不作为用户需要理解的新旋钮。用户只配置 `session_ttl`；server 内部根据 TTL 自动推导 cleanup 扫描周期：

```python
cleanup_interval = max(30, min(300, session_ttl // 2))
```

实施要求：

- `HTTPServiceServer` 启动时计算 `self.cleanup_interval`，lifespan 里的后台任务改为 `await asyncio.sleep(self.cleanup_interval)`，并在启动日志打印 `Session TTL` 与 `Cleanup interval`。
- `config_loader.py` 里现有的 `ServerConfig.cleanup_interval` 当前没有传到 `HTTPServiceServer`，属于空置配置。0.9 必须处理掉：优先删除该字段和 loader 读取；如果为了兼容旧配置而暂时保留，检测到用户显式配置 `cleanup_interval` 时必须 `warning`，说明该值已由 `session_ttl` 派生，不可静默无效。
- 验证测试必须覆盖派生值：`session_ttl=60` 时 cleanup interval 为 `30s`；`session_ttl=1800` 时为 `300s`；代码中不再出现用于 cleanup sleep 的硬编码 `300`。

## 13.6 执行注意事项与代码修订指导

本节是落实 §12 / §13 时的防踩坑清单。这里的规则优先级高于单个 commit 的局部描述；如果实施时发现冲突，先更新本文档再改代码。

### 13.6.1 session 生命周期必须成组落地

`heartbeat`、`session_ttl`、`cleanup_task`、`worker_disconnect` 不能只做其中一个：

- **0.4b** 负责把 `/heartbeat` 从 readonly 探针升级为 session lease keep-alive：server 收到 worker heartbeat 后刷新该 worker 名下 active sessions 的 TTL；client 默认开心跳。
- **2S.4** 负责并发治理：heartbeat jitter、HTTP client limits、websearch 共享线程池。不要在 0.4b 和 2S.4 里重复实现两套 jitter 逻辑。
- **0.7a / 2S.5** 负责主动释放：`Sandbox.close(destroy_sessions=True)`、worker disconnect、server shutdown 清 session。否则 `session_ttl=1800` 会把泄漏资源保留更久。
- **0.9** 负责 cleanup 机制治理：cleanup interval 由 `session_ttl` 派生，并处理 `cleanup_interval` 空置字段。

### 13.6.2 heartbeat 的临时状态提示

如果只把 client 侧 `auto_heartbeat` 改成 `True`，但 server `/heartbeat` 仍然只 `list_worker_sessions(worker_id)`，那它还不是续租机制，只是周期性状态探针。此状态允许作为过渡，但必须在代码注释、PR 描述或 commit message 中明确：

- 当前 heartbeat 会增加请求流量，但不会刷新 session TTL。
- 真正的 session 保活依赖 0.4b server 端 `refresh_session` 落地。
- 测试时不能把“heartbeat 请求成功”当成“session 不会过期”的证据；必须用 `session_ttl=60` + 两次 tool call 间隔 `sleep(90)` 的用例验证第二次 tool call 成功。

### 13.6.3 API 语义变化要显式记录

`/health` 仍保持 server liveness probe，只回答进程是否可服务，不读写 session。`/heartbeat` 改造后语义变为 worker/session keep-alive，会刷新 TTL。实施 0.4b 时必须同步：

- commit message 写明 `/heartbeat` 从 readonly status probe 升级为 session lease keep-alive。
- API 文档或 quick reference 写明请求方是 worker/client，参数必须包含 `worker_id`。
- response 增加或保留 `refreshed_sessions`，便于测试和排障。

### 13.6.4 不允许继续保留空置配置

配置字段只要对用户可见，就必须真的影响运行时行为。`config_loader.py` 里的 `cleanup_interval` 当前没有传到 `HTTPServiceServer`，属于反例。0.9 实施时按下面规则处理：

- 优先删除 `ServerConfig.cleanup_interval` 和 loader 读取逻辑，让 cleanup interval 完全由 `session_ttl` 内部派生。
- 如为了兼容旧配置暂时保留，必须在检测到用户配置 `cleanup_interval` 时打印 warning，说明该字段已废弃且不会覆盖派生值。
- 不允许静默读取但不生效，也不允许文档写“可配置”但代码未接通。

### 13.6.5 验收顺序建议

建议按下面顺序验证，避免局部通过掩盖生命周期问题：

1. 先用短 TTL 验证 0.4b：`session_ttl=60`、`heartbeat_interval=20`、两次 tool call 间隔 `sleep(90)`，第二次 tool call 必须成功。
2. 关闭 heartbeat 复现旧症状：同样 TTL 下 `auto_heartbeat=False`，第二次 tool call 应失败或 session not found。
3. 验证 2S.4 jitter：100 worker 的 `/heartbeat` 到达时间应分散在 24-36s 窗口内，无 30s 齐步尖峰。
4. 验证 0.7a / 2S.5：rollout 正常退出、Ctrl+C、server SIGTERM 都应尽快释放 session，而不是等 TTL。
5. 验证 0.9 cleanup：`session_ttl=60 -> cleanup_interval=30`，`session_ttl=1800 -> cleanup_interval=300`，后台任务使用派生值而非硬编码 `300`。

---

> **决策记录**：
>
> - v1.0（PARALLEL_INFER_PLAN.md）：协程级并发，被 GUI/VM 场景的 session 污染问题推翻。
> - v1.1（plan.md）：worker-pool + server 防风暴，方向正确但 Ctrl+C / 三层超时 / ulimit / 共享线程池等工程细节未明示。
> - v2.0（本文档）：把 v1.1 的所有设计 + 实战工程坑（ENG-1 到 ENG-20）合并成可执行 commit 清单。
> - v2.1（本文档 §12 增订，2026-05-18）：审计发现 ENG-21 到 ENG-34 共 14 个与 Commit 0.4 同性质的隐藏问题，作为补丁系列 Commit 0.4a–0.8d 落地，不改变 v2.0 主线 Phase 划分。
> - v2.2（本文档 §12.0.1 + §13 增订，2026-05-18）：对 §12 补丁做 reality check，多数 commit 降级或与其他 Phase 合并；新增 §13 汇总硬编码的运行时关键数值，并建议 Commit 0.9 把核心 magic number 暴露到 config。ENG-22 (heartbeat) 标记为 deferred，由 §13.4 调大 `session_ttl` 兜底。
> - v2.3（2026-05-19）：确认本轮也覆盖 GUI/VM/Browser，因此 Phase 2S 全部 5 个 commit 升为必做；0.7a/0.7b 随 Phase 2S 同批处理，并新增 GUI/VM/Browser smoke test 作为上线门禁。
> - v2.4（2026-05-19）：把 server 侧 1000 级高频并发的后置优化思路沉淀到 §14。当前 Phase 0-5 仍先把单进程 server 做稳；只有压测证明 server 单核/event loop 成为瓶颈时，再启动 §14 的 actor / supervisor / offload 方案。
> - v2.5（2026-05-19）：把 ENG-22 `/heartbeat` 从 deferred 升级为 P1。0.4b 进入 Phase 2S.4，同步实现 server heartbeat 刷 TTL、client 默认心跳和 jitter；`session_ttl=1800` 作为第二层兜底保留。
> - v2.6（2026-05-19）：`cleanup_task interval` 不再规划成用户配置项，改为 server 内部由 `session_ttl` 派生；同时要求清理 `config_loader.py` 中未接通的 `cleanup_interval` 空置字段，避免配置看似可调但实际不生效。
> - v2.7（2026-05-19）：新增 §13.6 执行注意事项与代码修订指导，明确 heartbeat / session_ttl / cleanup / worker_disconnect 必须成组落地；同时记录 client `auto_heartbeat=True` 只有在 server `/heartbeat` 刷 TTL 后才具备续租效果。

---

# 14. 后置方案：server 高频并发优化（Phase 6+）

> 本章只做设计沉淀，**不进入当前 Phase 0-5 必做范围**。当前优先级仍是：先完成单进程 rollout worker-pool + Phase 2S server 防风暴，让 100 并发与 GUI/VM/Browser 场景稳定可验收。只有压测发现 sandbox server 单核 CPU、event loop lag、JSON/result 包装或 actor 队列成为瓶颈时，才启动本章。

## 14.1 触发条件

满足任一条件，再考虑本章方案：

- sandbox server 单进程 CPU 长时间 > 80%，其他核心空闲。
- event loop lag p99 > 100ms，且 `/health` p99 或 `/execute` queue_wait_ms 明显抖动。
- 轻工具高并发（如 512/1000 in-flight）下，瓶颈不在 API provider、不在 rollout，而在 server 侧路由/包装/日志/JSON。
- RAG index 或 VM/Browser 这类异构资源无法用“多个完整 sandbox server”简单复制或平均分片。

## 14.2 不采用的方向：多个完整 sandbox server 平均分流

多个完整 server + 简单 hash / round-robin 不适合当前异构后端：

- VM pool 不能简单平均切分。例如总共 10 个 VM，5 个 server 每个 2 个 slot 时，sticky hash 容易导致局部满载。
- RAG 大 index 不能在每个 server 里各加载一份，否则内存会被重复占满。
- `ResourceRouter` / session 表是进程内状态，直接开 `uvicorn --workers N` 会让 `create_session` 和 `execute` 落到不同 worker，破坏 session 一致性。

如果未来确实要多 server，只能做 resource-aware routing，而不是简单“共享端口 + round-robin”。

## 14.3 推荐方向：单 HTTP 入口 + 异构 backend actors

保留一个外部 HTTP 入口和一个 session 控制面，把重资源执行层拆成配置驱动的 actor / pool：

```text
rollout
  |
  v
single sandbox HTTP server
  |
  |-- ResourceRouter / session truth
  |-- lightweight ProcessSupervisor
  |
  |-- VMActorPool          # 管 VM slot，不复制 VM pool
  |-- BrowserActorPool     # 管 browser session
  |-- RAGActor             # 单实例加载大 index
  |-- WebSearchThreadPool  # 轻工具共享线程池
  |-- CPUProcessPool       # 大 result 裁剪 / JSON / 统计等 CPU-heavy 后处理
```

核心原则：

- `ResourceRouter` 仍是唯一 session truth。
- actor 不直接对 rollout 暴露 HTTP；外部请求只打 sandbox HTTP server。
- `ProcessSupervisor` 只管 actor 生命周期、health/ready、crash 处理、metrics 汇总，不走每个 tool call 的大 payload 数据路径。
- 高频数据路径由 HTTP route 通过 `ActorClient` 直接调用目标 actor。
- 创建 session 时把 `(worker_id, resource_type)` 绑定到 `actor_id + slot_id`，后续 execute 直接读绑定，不每次问 supervisor。

## 14.4 Supervisor 边界

`ProcessSupervisor` 负责低频控制面：

- 按配置启动 actor。
- 等待 actor ready。
- 心跳检测与异常退出处理。
- shutdown 顺序清理。
- 汇总 actor queue depth / capacity / health 到 `/status`。

`ProcessSupervisor` 不负责高频数据面：

- 不转发每个 tool call 的大 payload。
- 不做 result 格式化/裁剪。
- 不做长时间排队。
- 不作为每次 execute 的同步路由裁决点。

错误架构：

```text
all requests -> ProcessSupervisor -> actors
```

推荐架构：

```text
HTTP route -> ResourceRouter binding -> ActorClient -> actor
```

## 14.5 配置驱动 actor topology

server 启动时按配置决定起哪些 actor，支持不同机器 profile：

```json
{
  "actors": {
    "enabled": true,
    "definitions": {
      "rag_main": {
        "type": "rag",
        "mode": "process",
        "enabled": true,
        "config": {
          "index_path": "/data/indexes/wiki.faiss",
          "batch_size": 64
        }
      },
      "vm_pool": {
        "type": "vm",
        "mode": "process",
        "enabled": true,
        "pool_size": 10,
        "config": {
          "provider": "docker",
          "headless": true
        }
      },
      "websearch": {
        "type": "websearch",
        "mode": "thread_pool",
        "enabled": true,
        "max_workers": 16
      }
    },
    "routing": {
      "rag": "rag_main",
      "vm": "vm_pool",
      "browser": "vm_pool",
      "websearch": "websearch"
    }
  }
}
```

原则：

- actor 默认关闭，保持现有 in-process backend 兼容。
- resource routing 显式配置，不靠名字猜。
- required actor 启动失败时 server 不 ready；optional actor 失败时 server 标 degraded。
- 第一版只支持启动时加载，不做 hot reload。

## 14.6 IPC 与协议

第一版建议使用本机 Unix domain socket JSON-RPC；也可以用 loopback TCP，但只允许本机访问。

actor 最小协议：

```text
health
ready
create_session
execute
destroy_session
shutdown
metrics
```

统一返回结构：

```json
{
  "ok": true,
  "code": 0,
  "message": "",
  "data": {},
  "metrics": {
    "queue_depth": 3,
    "execution_time_ms": 120
  }
}
```

大 result 不应经 supervisor 转发；必要时 actor 返回引用（临时文件 / shared memory handle）或先做截断。

## 14.7 按资源类型的执行策略

VM / Browser：

- 使用 slot-aware actor pool。
- `worker_id/session_id -> slot_id` 绑定由 ResourceRouter 记录。
- 同一 session 内操作串行，不同 slot 并行。
- actor 崩溃后相关 session 标记 failed，不静默复用。

RAG：

- 大 index 用 singleton actor 或专用 RAG service，避免多份内存复制。
- 内部保留 QueryBatcher。
- 高并发 query 走队列 + batch，队列满返回 429。

WebSearch / HTTP tools：

- 使用共享 `ThreadPoolExecutor` / `httpx.AsyncClient`。
- 限制 `max_workers / max_connections`。
- retry 使用 exponential backoff + jitter。

CPU-heavy 后处理：

- 大 JSON、result 裁剪、压缩、统计可放入 `ProcessPoolExecutor`。
- 小结果不要 offload，避免 IPC 开销大于收益。

## 14.8 观测与验收

启动本章前，先在 Phase 2S 压测中加观测：

- server event loop lag p50/p95/p99。
- server 单进程 CPU 使用率。
- actor queue depth / queue_wait_ms。
- result size 分桶。
- `/health` p99。
- `/execute` p95/p99。
- 429 / timeout / retry 次数。

本章验收建议：

- 512/1000 轻工具 in-flight 下，server event loop lag p99 < 100ms。
- `/health` p99 < 20ms。
- actor queue 满时快速返回 `429 + Retry-After`，不无界排队。
- RAG index 只加载一次或只在专用 actor/service 中加载。
- VM/Browser session cleanup 在 actor crash / server shutdown 后可验证。

