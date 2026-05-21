# L5 - fault injection

> 目标：把 Ctrl+C、kill -9、长 init、网络抖动等真实事故塞给已经跑起来的
> rollout / sandbox，确认它们能优雅退出，并且磁盘上没有半截 jsonl 行、
> 没有半截 checkpoint、没有泄漏的 server session。
>
> 这一层是**手工**为主，因为故障的时机本身就是要变量。

## 准备

```bash
bash start_sandbox_server.sh &
SERVER_PID=$!
export OPENAI_API_KEY=... OPENAI_BASE_URL=...
```

## 场景 1：单次 Ctrl+C（graceful）

```bash
# 起一个不会立刻结束的任务（比如 100 个 task）
python infer.py --config configs/infer/rag_infer.parallel.json \
    --output-dir /tmp/fault_ctrlc \
    --max-tasks 100 --parallel --max-workers 10 &
INF_PID=$!
sleep 8
kill -INT "$INF_PID"   # 第一次 Ctrl+C
wait "$INF_PID"
```

通过判据：
- `results_*.jsonl` 行数 < 100 但 > 0；每一行都能 `json.loads`。
- 终端有 `graceful shutdown started` 警告。
- sandbox server 端 `active_sessions` 在 30s 内回 0（worker.stop 触发了 destroy_session）。
- 没有 `Cannot start rollout: results file is locked`。

## 场景 2：连按 3 次 Ctrl+C（force exit）

```bash
python infer.py ... &
INF_PID=$!
sleep 5
kill -INT "$INF_PID"; sleep 0.5
kill -INT "$INF_PID"; sleep 0.5
kill -INT "$INF_PID"
wait "$INF_PID" || echo "exit code: $?"
```

通过判据：
- 退出码 130（`os._exit(130)`）。
- 终端依次出现 1/3、2/3、`Force exit on signal SIGINT`。
- `results_*.jsonl` 仍然每行 valid JSON（atomic append 起作用）。

## 场景 3：kill -9 跑到一半的 rollout

```bash
python infer.py ... --checkpoint-enabled &
INF_PID=$!
sleep 10
kill -9 "$INF_PID"
```

通过判据：
- `checkpoints/<run_id>/` 下每个未完成 task 留有一个完整的 `<task_id>.json`，
  没有 `.chkpt-*.tmp` 残留。
- `results_*.jsonl` 已写入的部分仍然每行 valid JSON。

## 场景 4：长 init 隔离

让 server 端某个 backend 在 `init` 时 sleep 30s（手工改 RAG initializer），
然后启 10 个 worker：

通过判据：
- 第 1 个 worker 等 ~30s 拿到 session；其余 9 个 worker 几乎立刻拿到（因为
  ResourceRouter.split lock）。
- 在那 30s 内，对 server 打 `curl /health` / `/api/v1/server/status` 应在 50ms
  内返回 200（因为长 init 已经在锁外完成）。

## 场景 5：网络抖动

```bash
# 用 iptables 临时 DROP 几秒 server 的入站，看 client 是否退避后恢复。
sudo iptables -A INPUT -p tcp --dport 8080 -j DROP
sleep 4
sudo iptables -D INPUT -p tcp --dport 8080 -j DROP
```

通过判据：
- client 端日志依次出现 `request POST /api/v1/execute failed (attempt N/3): ...; retry in X.XXs`。
- 抖动结束后任务继续推进，没有整个 worker 死掉。
- jitter 让多个 worker 的重试时间不集中（看时间戳应有 ±30% 抖动）。

## 场景 6：results 文件被另一个进程持锁

```bash
# 终端 A：
python -c "from rollout.core.result_store import ResultStore; \
    rs = ResultStore('/tmp/locked.jsonl'); rs.acquire_lock(); \
    import time; time.sleep(60)"

# 终端 B：
python infer.py --config ... --output-dir /tmp \
    --output-filename locked.jsonl  # 同一文件
```

通过判据：
- 终端 B 立即报错 `Cannot start rollout: results file is locked by another process`
  并退出（不阻塞、不静默写入）。
