# `feat/parallel-infer-v2` 渐进式测试套件

本目录是为了**逐步验证 `feat/parallel-infer-v2` 分支上 31 个 commit 的正确性**而新增的、不动主代码、可独立运行的测试。

> 设计目标：先用 5 分钟跑完 L0+L1 抓出 80% 回归；再用 1 小时跑 L2 抓接缝问题；L3–L5 是真实环境/压力/故障注入，按需启动。

## 分层一览

| 层级 | 名字 | 依赖 | 跑完时间 | 覆盖 |
| --- | --- | --- | --- | --- |
| **L0** | `L0_smoke` | 纯 import + 静态字段 | ~5 s | 配置/模块层面没有 typo、新字段在 `from_dict/to_dict/validate` 三处齐全 |
| **L1** | `L1_unit` | stub / mock，无网络、无 LLM、无 sandbox server | 30–60 s | 每个 commit 的核心不变量（per-worker logger、ShutdownManager、cancel-safe 保存、trace_id、三层超时、AsyncOpenAI、worker-pool 调度、ResourceRouter 拆锁、分层背压、serial lock、heartbeat lease、result store 锁、resume、checkpoint…） |
| **L2** | `L2_integration` | in-process stub sandbox（无外部进程） | 1–3 min | `RolloutPipeline._run_parallel` 端到端跑通、并发隔离、resume + checkpoint 路径 |
| **L3** | `L3_minirun` | 真实 sandbox server + 真实 LLM（最少量任务） | 5–10 min | 一次完整的 RAG / Web 推理 smoke |
| **L4** | `L4_concurrency` | 真实 sandbox server + 真实 LLM（100 路并发） | 30+ min | 真实并发吞吐、背压、序列化锁、内存/连接稳态 |
| **L5** | `L5_fault` | 真实 sandbox server（注入故障） | 手工 | Ctrl+C / kill -9 / 长 init / 网络抖动下的优雅退出与一致性 |

## 推荐使用顺序

```bash
cd /home/yanguochen/workspace/new_AF/AgentFlow
bash tests/parallel_infer/run_all.sh L0     # 30 行输出，必须全绿
bash tests/parallel_infer/run_all.sh L1     # 每个 commit 的核心不变量
bash tests/parallel_infer/run_all.sh L2     # 起一个 in-process stub server 跑端到端

bash tests/parallel_infer/run_all.sh        # 跑 L0 + L1 + L2（默认）
```

> L3–L5 需要真实环境，单独看每层下的 `README.md`。

## 退出码 / 报告

`run_all.sh` 会汇总每层退出码并把结果打到 `tests/parallel_infer/.last_run.log`。任何一层非零都会让脚本最终以非零退出，方便 CI 集成。

## 跑测试需要安装的依赖

仅依赖项目本身已经 require 的 `pytest`，外加：

```bash
pip install pytest pytest-timeout httpx
```

L2 用 in-process FastAPI server 时需要 `httpx`（你已经在 `requirements.txt` 中）。L3–L5 视具体场景决定额外依赖。

## 与 commit 的对应表

| Commit / 主题 | 在哪测 |
| --- | --- |
| 0.1 structured logging (`logging_utils.py`) | `L1_unit/test_phase0_observability.py::test_context_vars_across_await` 等 |
| 0.2 `ShutdownManager` | `test_phase0_observability.py::test_shutdown_event` 等 |
| 0.3 cancel-safe save | `test_phase0_observability.py::test_atomic_save_under_concurrency` |
| 0.4 `trace_id` 全链路 | `test_phase0_observability.py::test_trace_id_*` |
| 0.5 三层超时 | `test_phase0_observability.py::test_three_tier_timeout_*` |
| 0.4a `format_tool_result` 走干净路径 | `test_phase0plus_audit.py::test_format_for_llm_*` |
| 0.4b heartbeat 真续租 | `test_phase2s_sandbox.py::test_heartbeat_*` |
| 0.4c-a/b `_classify_tool_error`/`BdbQuit` | `test_phase0plus_audit.py::test_bdb_quit_*`, `test_misc.py::test_classify_*` |
| 0.4d evaluator score 三态 | `test_phase0plus_audit.py::test_evaluator_score_states` |
| 0.4e `effective_parameters` 全路径 | `test_phase0plus_audit.py::test_effective_parameters_*` |
| 0.4f duplicate task_id 三模式 | `test_phase0plus_audit.py::test_duplicate_task_id_*` |
| 1.1 / 1.2 `AsyncOpenAI` | `test_phase1_async_client.py` |
| 0.8a tool_calls paired | `test_phase2_worker_pool.py::test_tool_calls_paired` |
| 2.1 / 2.2 / 2.3 / 2.4 worker pool | `test_phase2_worker_pool.py` |
| 2S.1–2S.5, 0.7b | `test_phase2s_sandbox.py` |
| 3.1 / 3.2 / 3.3 文件锁/resume/checkpoint | `test_phase3_persistence.py` |
| 0.7d `extract_final_answer` last-match | `test_misc.py::test_last_match` |
| 0.9 cleanup_interval 派生 | `test_misc.py::test_derived_cleanup_interval` |
