# L4 - real concurrency load

> 目标：在真实 sandbox + 真实 LLM 上跑**几十到一百个并发任务**，
> 确认 worker-pool / 背压 / 心跳 / 文件锁在量大时仍然稳。
> 这一层是**有 LLM 配额成本的**，按需运行。

## 推荐方案

直接复用仓库内的 sample config，加大任务数：

```bash
# 100 路 RAG 并发，500 任务
cp configs/infer/rag_infer.parallel.json /tmp/rag.json
jq '.concurrency = 100 | .number_of_tasks = 500' \
   configs/infer/rag_infer.parallel.json > /tmp/rag.json

bash start_sandbox_server.sh --config configs/sandbox-server/rag_config.json &
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=...

python -m rollout.pipeline --config /tmp/rag.json \
    --api-key "$OPENAI_API_KEY" \
    --base-url "$OPENAI_BASE_URL" \
    --output-dir /tmp/rag_out \
    --no-eval
```

## 同步运行 `load_test.py`（数据面探针）

```bash
python tests/parallel_infer/L4_concurrency/load_test.py \
    --base-url http://127.0.0.1:18890 \
    --workers 100 --duration 30 \
    --resource-type rag --tool rag:search
```

这个脚本不依赖 LLM，直接打 sandbox `/api/v1/execute`，
用来定位「server 端是否扛得住 100 并发」。

## 通过判据

### `python -m rollout.pipeline` 跑完后

- `len(results.jsonl) == number_of_tasks`
- `tool_stats.success_rate >= 0.9`
- 终端日志：每个 worker 都有自己的 `rollout.worker.rollout_<run_id>_w<NNN>.log`
- sandbox server `/api/v1/server/status` 应该返回 `active_sessions = 100`，
  并在 `run` 结束后回落到 0（destroy_sessions=True 起作用）。
- 没有 `Cannot start rollout: results file is locked` —— fcntl 锁未误触发。

### `load_test.py` 跑完后

- 没有 5xx；如果有 429，必须看到 `Retry-After` 而且最后还是成功。
- 平均延迟与 `--workers 1` 相比放大 < N，证明背压在工作。
- `/api/v1/server/status` 报告的 `backpressure.{global,health,status,session_create,tool}`
  里 `rejected` 大于 0 才说明压力到位；如果一直是 0，需要继续加压。

## 失败时如何排查

1. **服务端 502 / connection refused 集中爆发** → `global_inflight` 太小，
   或 OS 句柄不够；`ulimit -n 65535 && bash start_sandbox_server.sh --config configs/sandbox-server/rag_config.json`。
2. **大量 5xx + client 反复重试** → backpressure 没正确分流；检查
   `BackpressureManager.global_inflight` 是否被 routes 引用。
3. **某个 worker 卡死 N 分钟** → 长任务 + 没设置 `task_max_seconds`；
   把它降回 1800 重跑。
4. **多个 worker 抢同一 vm session** → server 应通过 `_serial_guard`
   串行，而不是抛错；如果看到 backend 报 "concurrent call"，说明 2S.3 没生效。
