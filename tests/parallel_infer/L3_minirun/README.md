# L3 - mini smoke run

> 目标：用**真实 sandbox server + 真实 LLM**完整跑一次极少量任务，
> 端到端确认推理链路没有死掉。比 L0/L1/L2 多消耗 LLM 配额 + 1 个端口。

## 前置

```bash
# 1. sandbox server 已经能起得来（不是 docker，是本地启）
bash start_sandbox_server.sh   # 起在 0.0.0.0:8080

# 2. 在另一个终端，准备一个 LLM 端点（OpenRouter / 自建 vLLM 都可以）
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"

# 3. 准备最小数据集（仓库自带）
ls seeds/rag-mini  # 或自己造一个 2-3 行的 jsonl
```

## 运行

```bash
bash tests/parallel_infer/L3_minirun/run_smoke.sh rag    # RAG 场景
bash tests/parallel_infer/L3_minirun/run_smoke.sh web    # Web 场景
```

## 通过判据

- `results_*.jsonl` 行数 == 输入任务数。
- 至少一半任务 `success=true`（如果 LLM/sandbox 异常，会全 false）。
- `summary_*.json` 里 `tool_stats.total >= 1`、`tool_stats.success_rate > 0`。
- 终端无 traceback；只允许 `WARNING` / `INFO`。

## 失败时如何排查

1. **`results_*.jsonl` 完全没生成** —— `output_filename_strategy` 没生效，或者
   pipeline 在 `validate()` 阶段就挂了：看 stderr 里的 ValueError。
2. **`success=false` 全军覆没且 error 都是 `connect`** —— sandbox server 没起；
   `curl http://127.0.0.1:8080/health` 应返回 200。
3. **`success=false` 全军覆没且 error 都是 `task_timeout`** —— `task_max_seconds`
   太小，或者 LLM 拉不动；先把这个值临时调到 1800。
4. **`tool_stats=null`** —— 0 个 tool call，可能 prompt 没让模型用工具。
   先按 `configs/infer/web_infer.parallel.json` 这套配置跑。
