# 并发 Inference 调优指南

> 适用于 Phase 0 — Phase 3 完成后的 rollout / sandbox 栈。

本指南给出 **Web、RAG、GUI/VM/Browser** 三种典型工作负载的推荐配置，并提供一份「现象 → 大概率原因 → 排查路径」的速查表。

完整的设计与实施细节请参考 [`PLAN.md`](../../../PLAN.md)。

---

## 1. 怎么开起来

最小变更：把现有的 `configs/infer/<bench>_infer.json` 切换为 `configs/infer/<bench>_infer.parallel.json` 即可。

```bash
python -m rollout.pipeline --config configs/infer/web_infer.parallel.json
```

每份 sample 已经把所有新配置项注释好；按住一个配置文件不动也能继续跑（默认 `concurrency=1`、`parallel=false`、`output_filename_strategy=timestamp`，等价于改造前的串行行为）。

---

## 2. 三份推荐配置

### 2.1 Web（受 Serper / Jina 限速约束）

`configs/infer/web_infer.parallel.json`

| 项 | 推荐 | 说明 |
|----|------|------|
| `concurrency` | 16 | Serper 默认 30 QPS，给 LLM 留余量 |
| `worker_startup_jitter` | 1.0 | 16 个 worker 同时连 server 几乎没风暴 |
| `llm_max_connections` | 64 | 每 worker 一路 LLM + 缓冲 |
| `llm_timeout` | 120 | gpt-4-class 一次普通调用 |
| `task_max_seconds` | 600 | 多步搜索 + 浏览的安全上限 |
| `tool_default_timeout` | 60 | 单次 search / visit |

### 2.2 RAG（QueryBatcher 友好）

`configs/infer/rag_infer.parallel.json`

| 项 | 推荐 | 说明 |
|----|------|------|
| `concurrency` | 96 | server 端 QueryBatcher 设计就吃并发；瓶颈在 LLM |
| `worker_startup_jitter` | 3.0 | 拉开 RAG backend 的 warmup |
| `llm_max_connections` | 256 | 不能成为新的瓶颈 |
| `llm_timeout` | 180 | RAG 答案通常需要更长生成 |
| `task_max_seconds` | 900 | 长 reasoning 的兜底 |
| `tool_default_timeout` | 30 | RAG 检索很快 |

### 2.3 GUI / VM / Browser（受物理资源限制）

`configs/infer/gui_infer.parallel.json`

| 项 | 推荐 | 说明 |
|----|------|------|
| `concurrency` | 8 | 每 worker = 1 个 VM/Browser 进程 |
| `worker_startup_jitter` | 10.0 | VM 启动 30s 级，必须错峰 |
| `worker_startup_batch_size` | 4 | 每批 4 个起 |
| `worker_startup_batch_interval` | 30 | 上一批起完再下一批 |
| `tool_timeout_overrides` | `{vm:start: 300, vm:screenshot: 30, ...}` | per-tool 预算 |
| `task_max_seconds` | 1800 | GUI 任务通常更长 |
| `shutdown_timeout` | 60 | VM 销毁不要太急 |

> **注意**：server 端会对 `vm/browser/bash/code/mcp` 自动执行 per-(worker, resource_type) 串行锁（Phase 2S.3），同一 worker 不会出现 click+screenshot 并发，但需要确保 rollout 端 worker_id 唯一（Phase 2.2 已自动 `rollout_<run_id>_w<idx>`）。

---

## 3. 关键配置项速查（按 Phase 分组）

### Phase 0 — 三层超时 + 日志

| 配置 | 默认 | 何时调 |
|------|------|--------|
| `log_level` | `INFO` | 调试可改 `DEBUG` |
| `shutdown_timeout` | 30 | 单 worker cleanup 慢就调大 |
| `task_max_seconds` | 1800 | 单题最长执行时间 |
| `llm_timeout` | 120 | 单次 chat completion 上限（每次重试都计） |
| `tool_default_timeout` | 60 | 单次工具调用 |
| `tool_timeout_overrides` | `{vm:start: 120, browser:start: 60}` | per-tool override |

### Phase 1 — AsyncOpenAI 连接池

| 配置 | 默认 | 何时调 |
|------|------|--------|
| `llm_max_connections` | 256 | 上游 LLM 网关能承载多少并发 |
| `llm_max_keepalive` | 64 | 通常取 `max_connections / 4` |
| `llm_connect_timeout` | 15 | 链路时延高的话调大 |

### Phase 2 — Worker-pool 调度

| 配置 | 默认 | 何时调 |
|------|------|--------|
| `parallel` | `false` | 切到并行模式的开关 |
| `concurrency` | 1 | 真正的 worker 数（每 worker 独占 worker_id + sandbox session） |
| `worker_startup_jitter` | 3.0 | 大并发起多个 worker 时避免风暴 |
| `worker_startup_batch_size` | 0 | `>0` 时按批起 worker |
| `worker_startup_batch_interval` | 5.0 | 批间隔 |
| `fail_fast` | `false` | 第一个失败就触发整池 graceful shutdown |
| `keep_results_in_memory` | `true` | 大规模 run 关掉省内存（Phase 5 才能用 evaluator） |
| `per_worker_log` | `false` | 开启后每 worker 一个 `logs/<run_id>/rollout.worker.<wid>.log` |

### Phase 3 — 断点续推

| 配置 | 默认 | 何时调 |
|------|------|--------|
| `output_filename_strategy` | `timestamp` | 想要 resume 必须设为 `stable` 或 `explicit` |
| `output_filename` | `null` | `explicit` 模式下手动指定 |
| `resume` | `false` | 二次启动想接着跑就打开 |
| `resume_file` | `null` | 如果 prev 跑写到了别的文件 |
| `resume_retry_failed` | `true` | True=重跑失败行；False=连失败也跳过 |
| `on_duplicate_task_id` | `error` | benchmark 数据自检 |

### Sandbox 配置（同步随 Phase 2S/0.4b/0.7b 而来）

| 配置（`HTTPClientConfig`） | 默认 | 何时调 |
|------|------|--------|
| `auto_heartbeat` | `true` | 长思考 / 长工具调用一定要开 |
| `heartbeat_interval` | 30 | 配合 `session_ttl` |
| `heartbeat_jitter_ratio` | 0.2 | 防 100 worker 心跳同步打 server |
| `retry_backoff` | 2.0 | 指数底数 |
| `retry_jitter` | 0.3 | 防 100 worker 同步重试 |
| `max_connections` / `max_keepalive_connections` | 64 / 16 | 每 worker 对 server 的并发上限 |

服务端 `DEFAULT_SERVER_CONFIG.server.session_ttl` 已经从 300s 提到 1800s。

---

## 4. 现象 → 排查表

| 现象 | 大概率原因 | 排查路径 |
|------|-----------|---------|
| `concurrency=100` 但实际只跑 ~32 路 | Phase 1 没起效（仍走默认线程池） | `python -c "from rollout.core.utils import create_async_openai_client; print(create_async_openai_client.__module__)"` 应该可 import；runner 创建时打 `client: AsyncOpenAI` |
| GUI session 状态错乱（A worker 看到 B 的截图） | `worker_id` 共享了 | 看 server log 中 `worker_id=` 应该是 `rollout_<run_id>_w<idx>`，每个 worker 不同 |
| 启动后 5 分钟才开始跑题 | Phase 2S.1 没起效，ResourceRouter 锁未拆 | server log 看 `📦 Session CREATED` 是否真并发；10 worker 同时 create_session("rag") 应在 ≤ init×1.5 秒内全部完成 |
| 频繁 502 / timeout | 服务端过载 | server 日志找 `overloaded:` 行 → 缩小 `concurrency` 或调大 server `limits.*` |
| Ctrl+C 卡住或第二次 Ctrl+C 才退出 | `shutdown_timeout` 太短 / cleanup 不 cancel-safe | 调大 `shutdown_timeout`；按住 Ctrl+C 三次会强退（`os._exit(130)`） |
| 已经 60% 的题做完了挂了 | 没开 resume | 设 `output_filename_strategy: "stable"` + `resume: true` 后重启即可 |
| `tool` 角色 message 里全是 `code/meta/trace_id` 噪声 | 用了旧的 `format_tool_result_for_message`，Phase 0.4a 没走 | 看 runner 是否走 `_format_for_llm`；trajectory 中 tool message 内容应该是干净的 |
| 双进程都写同一 results 文件 | 没开 fcntl 锁 / 用了 timestamp 策略撞文件名 | 用 `output_filename_strategy=stable` 后第二个进程会立即 `Cannot start rollout: results file is locked by another process` |
| Heartbeat 在 server 端形成 30s 周期尖峰 | `heartbeat_jitter_ratio=0`（关掉了 jitter） | 恢复默认 0.2，或把 `concurrency` 拆到多个 server |
| Server `cleanup_expired` 把还在跑的 session 杀了 | `session_ttl` 太短 + 没开 client `auto_heartbeat` | 升级到 v2.7（默认 ttl=1800 + heartbeat 真续 TTL）；老配置可临时调大 server `session_ttl` 兜底 |

---

## 5. 操作系统层面建议

100 worker 单进程会撞文件句柄上限。建议运行前：

```bash
ulimit -n 65536
```

若开了 `per_worker_log=true`，每个 worker 还会持一个 rotating log fd，再加上 sandbox client 的 keepalive 连接，`ulimit -n` 设到 8192 起。

---

## 6. 关联设计文档

- `PLAN.md` — 落地到 commit 级的完整实施计划（v2.7）。
- `sandbox/docs/zh-CN/` — sandbox 服务端 API 与开发者文档。
- 本指南 — 用户视角的「我该怎么调」。

发现 sample 与文档不一致请提 PR：每个改动配置项的 commit 都要同步改 `configs/infer/*.parallel.json` 和本文档。
