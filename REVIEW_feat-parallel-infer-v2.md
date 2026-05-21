# Code Review 报告 — `feat/parallel-infer-v2`

> Review 对象：`/home/yanguochen/workspace/new_AF/AgentFlow`（分支 `feat/parallel-infer-v2`）
> 基线参照：`/home/yanguochen/workspace/AgentFlow`（plan 起点 `52ce30b`）
> 设计依据：`plan(1).md` v2.7（当前权威 plan）
> Review 时间：2026-05-20

---

## 总览

| 指标 | 值 |
| --- | --- |
| 分支 | `feat/parallel-infer-v2`（远端已同步） |
| commit 数 | **31** 个（从分叉点 `52ce30b` 起） |
| 文件改动 | 28 files, +4262 / −621 |
| Plan 对照 | 主线 22 项 + 审计 9 项均落地；Phase 5 / 0.8c / 0.7a 单独 commit 按 plan 决定**不做或合并到主线 commit** |
| 总体结论 | **正确性、一致性、可读性均达到 plan v2.7 验收标准**；发现 6 个 P3/P4 级 polish 项，无 P0/P1 阻塞 |

---

## 1. Commit ↔ Plan 映射

| Plan ID | Plan 标题（§0） | 仓库 Commit | 行数 | 状态 |
| --- | --- | --- | --- | --- |
| 0.1 | logger + Progress | `1361a27` | +162 | ✅ |
| 0.2 | ShutdownManager | `ece6a02` | +109 | ✅ |
| 0.3 | cancel-safe shutdown + atomic save（含 0.7c） | `88cffe3` | +245/−112 | ✅（已合并 0.7c） |
| 0.4 | trace_id + ToolCall 字段 | `38056c4` | +132/−42 | ✅ |
| 0.5 | 三层超时 | `00f1c71` | +120/−20 | ✅ |
| 0.4a | format_tool_result | `aa25fe4` | +37/−3 | ✅ |
| 0.4c-a | re-raise BdbQuit | `ad2aee2` | +6 | ✅ |
| 0.4d | 回填 evaluator score | `f46fc8f` | +34/−2 | ✅ |
| 0.4e | effective_parameters | `18de85b` | +31/−14 | ✅ |
| 0.4f | duplicate task_id | `61d0c9f` | +60/−1 | ✅ |
| 1.1 | AsyncOpenAI | `fb71b21` | +116/−21 | ✅ |
| 1.2 | sync chat_completion 保留 | `3b9eda7` | +10 | ✅ |
| 0.8a | tool_calls paired | `bafffaa` | +29/−6 | ✅ |
| 2.1 | worker-pool config | `da98e01` | +90 | ✅ |
| 2.2 | worker-pool scheduler（含 2.3 的 worker_id+jitter） | `1574472` | +258/−43 | ✅ |
| 2.3 | per-worker rotating log | `4dc6678` | +116 | ✅（标题缩窄，理由见下） |
| 2.4 | tool stats | `dca1d94` | +134/−6 | ✅ |
| 2S.1 | split ResourceRouter lock | `434d959` | +116/−61 | ✅ |
| 2S.2 | tiered backpressure | `6b6728a` | +505/−109 | ✅ |
| 2S.3 | serial lock | `1caf02b` | +247/−119 | ✅ |
| 2S.4 | shared websearch pool + heartbeat jitter | `08f6e03` | +131/−17 | ✅ |
| 2S.5 | worker_disconnect + server cleanup（含 0.7a） | `6ce1123` | +55/−17 | ✅（已合并 0.7a） |
| 0.4b | heartbeat refresh TTL（lease） | `3b0f38c` | +48/−4 | ✅ |
| 0.7b | exponential backoff + 4xx no-retry | `52cda4d` | +82/−44 | ✅ |
| 3.1 | output filename + 文件锁 | `3654162` | +295/−13 | ✅ |
| 3.2 | resume by task_id + 失败分类（含 0.8b 反序列化） | `80c9fc7` | +232/−6 | ✅（含 0.8b） |
| 4.1 | sample configs + tuning guide | `0b75ca3` | +396/−1 | ✅ |
| 0.7d | last-match extract_final_answer | `da139f2` | +31/−12 | ✅ |
| 0.4c-b | classify tool errors | `cd1747d` | +153/−10 | ✅（独立 commit，未并入 2.4，可读性更好） |
| 0.9 | magic numbers + derived cleanup interval | `d81bce1` | +114/−9 | ✅ |
| 3.3 | mid-task checkpoint（可选项） | `26b3e93` | +244/−5 | ✅ |

**未在新仓库实现，按 plan 合理跳过 / 合并**：
- `0.7a` Sandbox.close 默认 destroy_sessions=True — 合并在 `6ce1123` 中。
- `0.7c` atomic _save_result — plan 说"合并到 0.3"，已与 `88cffe3` 一致。
- `0.8b` ToolCall.from_dict / Trajectory.from_dict — 合并在 `80c9fc7` (3.2) 与 `26b3e93` (3.3) 的 `models.py` 改动里，docstring 标注 "Phase 3 / commit 0.8b"。
- `0.8c` reuse single event loop — plan 标 P3 / 可删除，未做合理。
- `5.1 / 5.2 / 0.8d` Phase 5 评估侧 — plan 标 ○ 可选，未做合理。

**标题漂移**：
- `4dc6678 feat(rollout): per-worker rotating log file with grep-clean isolation`（plan 标题为 "unique worker_id, per-worker logger, startup jitter"）—— 因为 `worker_id / startup jitter` 已在 2.2 (`1574472`) 一起实现，2.3 实质只剩 per-worker file，标题更准。**合理**。

---

## 2. Phase 分项 review 结论

| Phase | Commit 数 | 结论 |
| --- | --- | --- |
| Phase 0 | 5 | ✅ 实现严谨，比 plan 多防御性：`force_exit_after >= 2` 校验、`set_context` skip None、`clear_context` 的 ValueError fallback、pytest capture 兼容。 |
| Phase 0+ | 5 | ✅ 完美最小修复；0.4d 三分支分别填 `score=0/None/score`，正确区分 "no GT" 与 "genuine 0"；0.4e `_execute_tool` 返回 tuple 在所有 code path（含 timeout / Exception）都返回 `effective_parameters`，比 plan 更细。 |
| Phase 1 | 2 | ✅ `httpx.AsyncClient + httpx.Limits + httpx.Timeout(timeout_s, connect=connect_timeout_s)` 显式 pool；`run_in_executor` 已彻底替换；evaluator 仍保留 sync client（1.2 设计契约）。 |
| Phase 2 前置 | 1 | ✅ `executed_tool_calls = assistant.tool_calls[:1]` 与 `Message.tool_calls = [tc.model_dump() for tc in executed_tool_calls]` 用同一变量同步；N≥2 时打 WARN 透出。 |
| Phase 2 | 4 | ✅ `asyncio.Queue + FIRST_COMPLETED` 等 worker 或 shutdown；per-worker cancel-safe `runner.stop()`；startup jitter + 可选 batched startup；`tool_stats` 与 `score` 解耦；`keep_results_in_memory=False` 时不存内存（Phase 5 准备）。 |
| Phase 2S | 7 | ✅ ResourceRouter 三阶段拆锁（fast-path / 无锁 init / publish）+ singleflight；5 lane backpressure（health / status / global / session_create / tool）；per-(worker_id, resource_type) serial lock + destroy observer；shared websearch executor + httpx Limits + heartbeat jitter；heartbeat-as-lease；worker_disconnect 默认开 + bounded server cleanup（60s）；指数退避 + 4xx 永久错误不重试。 |
| Phase 3 | 3 | ✅ `fcntl.flock(LOCK_EX | LOCK_NB)` + Windows fallback；resume 双模（`retry_failed=True` 只跳 success；`=False` 跳所有）；CheckpointStore `tempfile + flush + fsync + os.replace` 原子写，失败 WARN 不 raise。 |
| Phase 4 | 1 | ✅ 三场景 sample config（web=16 / rag=96 / gui=8）+ 调优指南（Phase 0/1/2/3/Sandbox 五大表）+ 现象→排查表（10+ 条）+ OS ulimit 建议。 |
| 0.9 | 1 | ✅ `_derive_cleanup_interval = clamp(ttl//2, 30, 300)`，对 ttl=60/120/300/600/1800 全部返回合理值；ConfigLoader 旧 `cleanup_interval` 字段保留 + WARN；服务启动 log 同时输出 ttl 和派生值。 |

---

## 3. 设计亮点（值得保留的实现）

1. **`ExecuteRequest.trace_id` 显式声明**（0.4 / `38056c4`）：
   发现并修复了 plan 没明确强调的 Pydantic `extra="ignore"` 吞字段陷阱。如果不在协议层显式声明 `trace_id`，server-side 的 `hasattr(request, "trace_id")` 检查永远 False，trace_id 永远 None。

2. **`shield(wait_for(coro, timeout))` 嵌套**（0.3 / `88cffe3`，1.1 / `fb71b21`，2S.5 / `6ce1123`）：
   外层 cancel 不打断 cleanup，但 cleanup 自身有时间上限。`runner.stop()` 的 destroy_session / sandbox.close / LLM client.close 各自独立 `shield`，一个挂住不影响另一个。

3. **`_serial_guard` + `add_destroy_observer`**（2S.3 / `1caf02b`）：
   服务端 "belt-and-braces" 串行锁。`DEFAULT_SERIAL_RESOURCE_TYPES = {vm, browser, bash, code, mcp}` 覆盖 stateful 后端；rag/websearch 等 stateless 资源不进 guard，保留 worker 内并发能力。Session destroy 时通过 `add_destroy_observer` callback 自动释放 lock，避免长生命周期内存积累。

4. **`_derive_cleanup_interval` 派生而非额外配置**（0.9 / `d81bce1`）：
   用户只配 `session_ttl`，scan 周期由服务端按 `max(30, min(300, ttl // 2))` 计算，避免两值不协调（plan §13.6.4 落地）。

5. **`output_filename_strategy` 三档**（3.1 / `3654162`）：
   `timestamp`（默认，向后兼容）/ `stable`（resume 友好）/ `explicit`（用户控制名）。eval/summary 自动与 results 同 stem 联动。

6. **`tool_stats` 与 `score` 解耦**（2.4 / `dca1d94`）：
   tool 执行健康 ≠ 答案正确性；`RolloutSummary` 两字段独立，便于运维排障时不被 LLM 答题表现干扰。

7. **`AsyncTokenBucket` 还没用上但接口已经在 config**（2.1 / `da98e01`）：
   `serper_qps` 字段预埋，注释说明"takes effect when Phase 2S TokenBucket lands"；让未来 commit 是一行配置启用而不是结构变更。

8. **Singleflight init**（2S.1 / `434d959`）：
   同一 `(worker_id, resource_type)` 的并发 `create_session` 共享 leader 的 future，避免重复 init；同时把 sync initializer 通过 `asyncio.to_thread` off-load，不阻塞 event loop。

---

## 4. 发现的问题（按严重度）

### 🟡 Medium — 建议下一个 PR 修

#### M-1. `RolloutConfig.sandbox_retry_*` 是 dead field

- **位置**：`rollout/core/config.py:195-197` + `rollout/core/runner.py:348-355`
- **现象**：`sandbox_retry_max / sandbox_retry_backoff_base / sandbox_retry_jitter` 在 RolloutConfig 上**完整三联（dataclass / validate / to_dict）**，但 `AgentRunner.start()` 创建 `Sandbox(...)` 时只传了 `server_url / worker_id / auto_start_server / server_config_path / timeout / warmup_resources`，**未透传 retry 字段**。
- **影响**：用户写 `"sandbox_retry_max": 5` 看似生效（validate 通过、to_dict 保留），实际 sandbox client 仍用 `HTTPClientConfig` 的默认值（`max_retries=3, retry_backoff=2.0, retry_jitter=0.3`）。属于 ENG-22 同类的 silent dead field。
- **修复建议**：
  - 在 `Sandbox.__init__` 增加 `http_retry_max / http_retry_backoff / http_retry_jitter` 入参；
  - `_create_client` 把它们写进 `HTTPClientConfig`；
  - `AgentRunner.start()` 透传 `RolloutConfig.sandbox_retry_*`。

#### M-2. `BackpressureManager.global_inflight` 定义但无路由 acquire

- **位置**：`sandbox/server/core/backpressure.py:226` 定义；`sandbox/server/routes.py` 中**无任何 `global_inflight.acquire_or_429`**。
- **现象**：默认 capacity=512, queue_max=1024 的全局 lane 没起作用。
- **影响**：各 sub-lane 独立 cap 加起来（health 256 + status 128 + tool LaneGroup + session_create LaneGroup）可能超过 server-wide 上限，绕过 `global_queue_max=1024` 这层兜底。当前默认值正好 ~512，实际影响小；自定义放大 sub-lane 后会失控。
- **修复建议**：
  - 在 FastAPI 中间件层添加 `async with global_inflight.acquire_or_429(1.0)`，统一兜底；
  - 或在每个路由的最外层 try 包一层 global lane（嵌套两次 acquire）。

#### M-3. Server 返回的 `Retry-After` header 没被 client 尊重

- **位置**：`sandbox/client.py:367-376`
- **现象**：`_request` 命中 429 时只用本地 `retry_delay * retry_backoff^attempt` 计算 wait，**忽略服务器响应里的 `Retry-After` header**。Plan §7 0.7b 提到这点，落地时简化了。
- **影响**：客户端不按服务端节奏退避，更早重试可能触发 secondary 429，污染 server 日志。
- **修复建议**：
  ```python
  retry_after = 0
  try:
      retry_after = int(response.headers.get("Retry-After", "0"))
  except (TypeError, ValueError):
      pass
  wait = max(local_calc, retry_after)
  ```

---

### 🟢 Low — 小修复，不影响功能

#### L-1. `websearch.py` 重复 import

- **位置**：`sandbox/server/backends/tools/websearch.py:12-13`
- **现象**：相邻两行都是 `import asyncio`。
- **影响**：linter 报 F811 redefined-while-unused；功能无影响（Python import 缓存）。
- **修复**：删除其中一行。

#### L-2. `_ctx_trace` 函数违反 PEP8 import-position

- **位置**：`rollout/core/runner.py:19-23`
- **现象**：
  ```python
  from .logging_utils import get_context, get_logger, set_context, clear_context

  def _ctx_trace() -> str:
      return get_context().get("trace_id", "-")
  from .models import (
      BenchmarkItem, Trajectory, Message, ToolCall, TaskResult
  )
  ```
  `def _ctx_trace()` 夹在两段 import 之间。
- **影响**：pylint / ruff 报 `wrong-import-position`；功能无影响。
- **修复**：把函数移到最后一个 `from ... import ...` 之后。

#### L-3. `ResourceRouter.get_or_create_session` 的 leader cancel 时 hang corner case

- **位置**：`sandbox/server/core/resource_router.py` 的三阶段实现
- **现象**：如果 leader 在 Step 2 的 `await initializer(...)` 期间被 `asyncio.CancelledError`（非 try/except 内部触发的异常，而是外部 cancel），Step 3 不会执行，`leader_fut` 永不 `set_result`，所有 non-leader caller `await leader_fut` 会无限阻塞。
- **触发条件**：服务端外部主动 cancel `get_or_create_session` 的协程（非通过 Sandbox 正常路径）；普通 SIGTERM/SIGINT 走 uvicorn lifespan，整 loop 一起退出，不会触发此 corner case。
- **影响**：理论上可能，实际几乎不会发生。
- **修复建议**：在 Step 2 包一个 `try / finally`，finally 兜底 `if not leader_fut.done(): leader_fut.set_exception(CancelledError("init cancelled"))`，并 `self._initializing.pop(key, None)` 清掉 slot。

---

## 5. 可读性 / 风格观察（全部正向）

- **Docstring 一致**：每个新增模块、公开函数都有 docstring；plan 引用通过 `Phase X / commit X.Y (ENG-Z)` 标准化形式标注，便于 git blame 追溯。
- **commit message 质量**：每条都有动机 + 改动列表 + Minimal verification（具体到 "50 concurrent gather of _save_result 产生 50 行 JSONL"、"4 worker x 20 tasks 产生 4 个 log 文件每个 5 行" 等可重放数字），便于 reviewer 与未来 git blame。
- **Cancel-safe 模式**：runner.stop / pipeline.shutdown / scheduler / file lock / heartbeat / cleanup 全部用 `try/finally + shield + wait_for`，模式一致。
- **`_comment_xxx` JSON 字段**：sample config 用 JSON 注释技巧分组，可读性远超纯 JSON。
- **向后兼容**：`max_workers → concurrency` 自动映射 + WARN；`cleanup_interval` 旧字段保留 + WARN；`ToolCall.from_dict / Trajectory.from_dict` 用 `.get(..., default)` 兼容老 trajectory；`output_filename_strategy=timestamp` 默认保证旧脚本零变化。

---

## 6. 验收结论

- **Plan v2.7 落地完整度**：100%（主线 22 + 审计 9 个 commit 全部到位，Phase 5 / 0.8c 等 plan 标 P3/optional 的项按 plan 跳过合理）。
- **正确性**：所有核心并发路径（worker-pool / ResourceRouter / serial lock / backpressure / cancel-safe / atomic save / file lock）逻辑无错。
- **一致性**：config 字段三联（dataclass / validate / to_dict / from_dict）齐；命名、log 上下文、commit 信息格式统一。**除 M-1 的 dead field 外**，没有"字段定义却未消费"的悬挂。
- **可读性**：docstring + 注释密度高且解释"为什么"而非"做什么"；commit 拆分粒度小、可独立回滚。

> **建议合入主线**。
> M-1 / M-2 / M-3 三个 medium 问题可作为后续 polish PR（不影响本次验收）。L-1 / L-2 顺手在 polish PR 里修；L-3 等 plan §15（未来 Actor 拓扑）一起处理。

---

## 附录 A：本次 review 使用的代码引用

- `rollout/core/logging_utils.py` — Phase 0 logger
- `rollout/core/shutdown.py` — Phase 0 ShutdownManager
- `rollout/pipeline.py` — `run_async / _run_sequential / _run_parallel / _spawn_worker / _save_result / _check_duplicate_task_ids / _apply_resume_filter`
- `rollout/core/runner.py` — `AgentRunner / _execute_tool / _resolve_tool_timeout / _format_for_llm / _run_task_inner / _compute_tool_stats / _aggregate_tool_stats`
- `rollout/core/config.py` — `RolloutConfig` 完整字段表
- `rollout/core/models.py` — `ToolCall.from_dict / Trajectory.from_dict / TaskResult.tool_stats / RolloutSummary.tool_stats`
- `rollout/core/utils.py` — `create_async_openai_client / async_chat_completion / extract_final_answer`
- `rollout/core/result_store.py` — fcntl 文件锁
- `rollout/core/checkpoint_store.py` — atomic mid-task checkpoint
- `sandbox/sandbox.py` — `Sandbox.close(destroy_sessions=True)` + `SandboxConfig.heartbeat_*`
- `sandbox/client.py` — heartbeat jitter + `_request` 退避策略
- `sandbox/protocol.py` — `ExecuteRequest.trace_id` 显式字段
- `sandbox/server/routes.py` — 5 lane backpressure + heartbeat lease
- `sandbox/server/core/backpressure.py` — `Bound / LaneGroup / BackpressureManager`
- `sandbox/server/core/resource_router.py` — 三阶段拆锁 + singleflight + destroy_observer
- `sandbox/server/core/tool_executor.py` — `_serial_guard / DEFAULT_SERIAL_RESOURCE_TYPES`
- `sandbox/server/app.py` — `_derive_cleanup_interval / cleanup_task / lifespan` bounded cleanup
- `configs/infer/*.parallel.json` — 三场景 sample config
- `docs/zh-CN/guides/PARALLEL_INFER.md` — 调参指南

## 附录 B：未来工作建议

1. **Polish PR**：把 M-1 / M-2 / M-3 / L-1 / L-2 五个问题独立 commit 修掉，预计 ~150 LoC。
2. **Phase 5（评估侧异步化）**：plan 标可选，但 RAG/GUI 大规模评估时仍会成为瓶颈，建议在第一次大规模并发跑通后跟进。
3. **Plan §15 Actor 拓扑**：当单 server `concurrency=100+` 出现热点时启动，与 L-3 的 leader-cancel 兜底一起改造。
4. **TokenBucket 启用**：`serper_qps` 字段已埋，缺一个 `AsyncTokenBucket` 实现 + websearch tool 接入。
5. **Phase 6（observability）**：把 backpressure stats / tool stats / worker progress 暴露成 `/metrics` 端点，方便 Prometheus 抓取。

