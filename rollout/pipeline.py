#!/usr/bin/env python3
"""
Rollout Pipeline - Main execution pipeline for running agents on benchmarks

This module provides the main RolloutPipeline class that handles:
1. Loading benchmark data
2. Setting up agent runner with sandbox
3. Running agent on tasks (sequential or parallel)
4. Evaluating results
5. Saving outputs
"""

import json
import os
import sys
import uuid
import asyncio
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional
import time
from datetime import datetime
import concurrent.futures

from .core import (
    RolloutConfig,
    BenchmarkItem,
    TaskResult,
    RolloutSummary,
    Evaluator,
    load_benchmark_data,
    get_timestamp
)
from .core.runner import AgentRunner
from .core.logging_utils import (
    install_root_handler, get_logger, set_context, clear_context,
)
from .core.shutdown import ShutdownManager


log = get_logger("rollout.pipeline")


class RolloutPipeline:
    """
    Main Rollout Pipeline for benchmark execution.
    
    Usage:
        config = RolloutConfig.from_json("config.json")
        pipeline = RolloutPipeline(config)
        summary = pipeline.run()
    """

    def __init__(self, config: RolloutConfig, output_dir: Optional[str] = None):
        """
        Initialize pipeline.
        
        Args:
            config: Rollout configuration
            output_dir: Override output directory
        """
        self.config = config
        self.output_dir = output_dir or config.output_dir or "rollout_results"

        if self.config.trajectory_only:
            # Trajectory-only mode is for inference logging, so disable evaluation
            # and guarantee trajectory persistence in results output.
            self.config.evaluate_results = False
            self.config.save_results = True
            self.config.save_trajectories = True
        
        # Validate config
        errors = config.validate()
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        # Create output directory
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Initialize output files
        timestamp = get_timestamp()
        benchmark_name = config.benchmark_name or "benchmark"
        
        self.results_file = os.path.join(self.output_dir, f"results_{benchmark_name}_{timestamp}.jsonl")
        self.eval_file = os.path.join(self.output_dir, f"evaluation_{benchmark_name}_{timestamp}.json")
        self.summary_file = os.path.join(self.output_dir, f"summary_{benchmark_name}_{timestamp}.json")
        
        print(f"💾 Output files:")
        print(f"   Results: {self.results_file}")
        if self.config.evaluate_results:
            print(f"   Evaluation: {self.eval_file}")
        if self.config.save_summary:
            print(f"   Summary: {self.summary_file}")
        
        # Results storage
        self.results: List[TaskResult] = []
        self.benchmark_items: List[BenchmarkItem] = []

        # Per-run identity + shutdown plumbing (initialised lazily in
        # run_async so this object remains pickle-friendly).
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self._save_lock: Optional[asyncio.Lock] = None
        self._shutdown: Optional[ShutdownManager] = None

    def load_benchmark(self) -> List[BenchmarkItem]:
        """Load benchmark data"""
        if not self.config.data_path:
            raise ValueError("data_path not specified in config")
        
        print(f"\n📂 Loading benchmark from: {self.config.data_path}")
        
        raw_data = load_benchmark_data(self.config.data_path)
        
        # Convert to BenchmarkItem
        items = [BenchmarkItem.from_dict(item) for item in raw_data]

        # Duplicate task_id is a data-hygiene bug: results/evaluation/
        # checkpoint/resume all join on task_id, so duplicates silently
        # overwrite each other. Run the check on the *raw* item list
        # (before id-filter / number-of-tasks slicing) so users see the
        # actual condition of their dataset.
        self._check_duplicate_task_ids(items)

        # Filter by task_ids if specified
        if self.config.task_ids:
            task_id_set = set(self.config.task_ids)
            items = [item for item in items if item.id in task_id_set]
            print(f"   Filtered to {len(items)} specific tasks")
        
        # Limit number of tasks if specified
        if self.config.number_of_tasks is not None:
            items = items[:self.config.number_of_tasks]
            print(f"   Limited to first {len(items)} tasks")
        
        print(f"   Loaded {len(items)} tasks")
        self.benchmark_items = items
        return items

    def _check_duplicate_task_ids(self, items: List[BenchmarkItem]) -> None:
        """Enforce the ``on_duplicate_task_id`` policy.

        Mode is taken from ``RolloutConfig.on_duplicate_task_id``:
        ``"error"`` raises ``ValueError`` (default, fail-fast); ``"warn"``
        logs and continues; ``"ignore"`` is a no-op. Duplicate ids are
        reported in encounter order (truncated to 10 in the message).
        """
        mode = getattr(self.config, "on_duplicate_task_id", "error")
        if mode == "ignore":
            return

        seen = set()
        duplicates: List[str] = []
        for it in items:
            if it.id in seen:
                duplicates.append(it.id)
            else:
                seen.add(it.id)

        if not duplicates:
            return

        shown = duplicates[:10]
        more = "" if len(duplicates) <= 10 else f" (+{len(duplicates) - 10} more)"
        msg = (
            f"benchmark data contains {len(duplicates)} duplicate task_id(s): "
            f"{shown}{more}"
        )

        if mode == "error":
            raise ValueError(msg)
        if mode == "warn":
            log.warning(msg)
            print(f"⚠️  {msg}")

    async def run_async(self) -> RolloutSummary:
        """Run pipeline asynchronously"""
        install_root_handler(level=getattr(self.config, "log_level", "INFO"))
        ctx_tokens = set_context(run_id=self.run_id)
        start_time = time.time()

        # Per-loop primitives (must be created inside the running loop).
        self._save_lock = asyncio.Lock()
        self._shutdown = ShutdownManager()
        try:
            self._shutdown.install(asyncio.get_running_loop())
        except Exception as e:
            # Non-fatal: continue without signal handling (e.g. when run
            # inside a worker thread or notebook).
            log.warning("could not install ShutdownManager: %r", e)

        try:
            # Load benchmark
            if not self.benchmark_items:
                self.load_benchmark()

            print(f"\n{'='*80}")
            print(f"🚀 Rollout Pipeline (run_id={self.run_id})")
            print(f"{'='*80}")
            print(f"Total tasks: {len(self.benchmark_items)}")
            print(f"Model: {self.config.model_name}")
            print(f"Max turns: {self.config.max_turns}")
            print(f"Parallel: {self.config.parallel}")
            print(f"{'='*80}\n")
            log.info(
                "starting rollout: tasks=%d model=%s parallel=%s",
                len(self.benchmark_items), self.config.model_name, self.config.parallel,
            )

            # Create runner
            runner = AgentRunner(self.config, worker_id="main_runner", run_id=self.run_id)

            try:
                # Start runner
                print("Starting runner...")
                success = await runner.start()
                if not success:
                    raise RuntimeError("Failed to start runner")

                # Run tasks
                if self.config.parallel and self.config.max_workers > 1:
                    await self._run_parallel(runner)
                else:
                    await self._run_sequential(runner)

            finally:
                # Cancel-safe cleanup, bounded by shutdown_timeout.
                print("\n🔌 Stopping runner...")
                try:
                    await asyncio.shield(
                        asyncio.wait_for(
                            runner.stop(), timeout=self.config.shutdown_timeout
                        )
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    log.warning(
                        "runner.stop() did not finish within shutdown_timeout=%ss (%r); "
                        "server session may be cleaned by TTL",
                        self.config.shutdown_timeout, e,
                    )

            # Evaluate results
            evaluation = None
            if self.config.evaluate_results and self.results:
                print("\n📊 Evaluating results...")
                evaluator_model_name = self.config.evaluator_model_name or self.config.model_name
                evaluator_api_key = self.config.evaluator_api_key or self.config.api_key
                evaluator_base_url = self.config.evaluator_base_url or self.config.base_url
                evaluator = Evaluator(
                    metric=self.config.evaluation_metric,
                    model_name=evaluator_model_name,
                    api_key=evaluator_api_key,
                    base_url=evaluator_base_url,
                    temperature=self.config.evaluator_temperature,
                    max_retries=self.config.evaluator_max_retries,
                    extra_params=self.config.evaluator_extra_params,
                )
                evaluation = evaluator.evaluate(self.results)

                # Save evaluation
                with open(self.eval_file, 'w', encoding='utf-8') as f:
                    json.dump(evaluation, f, indent=2, ensure_ascii=False)
                print(f"   Evaluation saved to: {self.eval_file}")

                # Score sidecar: keep results_*.jsonl append-only (Phase 3
                # resume needs that) but emit a parallel scores file so
                # callers can sort/filter by score without joining JSON
                # structures. `evaluator.evaluate` has already written
                # `result.score` back, so this is just a projection.
                if self.config.save_results:
                    scores_file = self.results_file
                    if scores_file.endswith(".jsonl"):
                        scores_file = scores_file[:-len(".jsonl")] + ".scores.jsonl"
                    else:
                        scores_file = scores_file + ".scores.jsonl"
                    try:
                        with open(scores_file, "w", encoding="utf-8") as f:
                            for r in self.results:
                                f.write(json.dumps({
                                    "task_id": r.task_id,
                                    "success": r.success,
                                    "score": r.score,
                                }, ensure_ascii=False) + "\n")
                        print(f"   Scores written: {scores_file}")
                    except OSError as e:
                        log.warning("could not write scores sidecar %s: %r", scores_file, e)

            # Calculate summary
            total_time = time.time() - start_time
            successful = sum(1 for r in self.results if r.success)
            avg_score = evaluation.get("average_score", 0.0) if evaluation else 0.0

            summary = RolloutSummary(
                benchmark_name=self.config.benchmark_name or "benchmark",
                total_tasks=len(self.results),
                successful_tasks=successful,
                failed_tasks=len(self.results) - successful,
                average_score=avg_score,
                metric=self.config.evaluation_metric,
                total_time_seconds=total_time,
                results_file=self.results_file,
                evaluation_file=self.eval_file if self.config.evaluate_results else None
            )

            # Save summary (optional)
            if self.config.save_summary:
                with open(self.summary_file, 'w', encoding='utf-8') as f:
                    json.dump(summary.to_dict(), f, indent=2, ensure_ascii=False)

            # Print summary
            print(f"\n\n{'='*80}")
            print(f"🎉 Rollout Complete!")
            print(f"{'='*80}")
            print(f"Total tasks: {summary.total_tasks}")
            print(f"Successful: {summary.successful_tasks}")
            print(f"Failed: {summary.failed_tasks}")
            print(f"Average score: {summary.average_score:.3f}")
            print(f"Total time: {summary.total_time_seconds:.1f}s")
            print(f"Results: {self.results_file}")
            print(f"{'='*80}\n")

            return summary
        finally:
            clear_context(ctx_tokens)

    async def _run_sequential(self, runner: AgentRunner) -> None:
        """Run tasks sequentially.

        Honours ``ShutdownManager``: if a SIGINT/SIGTERM was received we
        stop pulling new tasks and let the enclosing ``run_async`` reach
        its ``finally`` block for cancel-safe cleanup.
        """
        for idx, item in enumerate(self.benchmark_items, 1):
            if self._shutdown is not None and self._shutdown.triggered:
                log.warning(
                    "graceful shutdown requested; stopping after %d/%d tasks",
                    idx - 1, len(self.benchmark_items),
                )
                break

            print(f"\n[{idx}/{len(self.benchmark_items)}]", end=" ")

            ctx_tokens = set_context(worker_id=runner.worker_id, task_id=item.id)
            try:
                result = await runner.run_task(item)
            finally:
                clear_context(ctx_tokens)
            self.results.append(result)

            # Save result immediately (atomic append + fsync, see _save_result).
            if self.config.save_results:
                await self._save_result(result)

    async def _run_parallel(self, runner: AgentRunner) -> None:
        """Run tasks in parallel using thread pool"""
        # Note: For true parallelism, we'd need multiple runner instances
        # This implementation uses sequential execution with async for simplicity
        # True parallel would require multiple sandbox sessions
        
        print(f"⚠️ Parallel mode with max_workers={self.config.max_workers}")
        print("   Note: Using sequential execution (parallel requires multiple sandbox sessions)")
        
        await self._run_sequential(runner)

    def _build_result_payload(self, result: TaskResult) -> Dict[str, Any]:
        """Materialise the dict that will be appended to results.jsonl."""
        if self.config.trajectory_only:
            payload: Dict[str, Any] = {
                "task_id": result.task_id,
                "success": result.success,
                "trajectory": result.trajectory.to_dict() if result.trajectory else None,
            }
            if result.error:
                payload["error"] = result.error
            return payload
        return result.to_dict()

    async def _save_result(self, result: TaskResult) -> None:
        """Append a TaskResult as a single JSON line.

        Concurrency safety:
        - serialises calls with ``self._save_lock`` so two coroutines
          can never interleave bytes inside one line;
        - performs the actual write in a worker thread via
          ``asyncio.to_thread`` so the event loop is never blocked on disk
          IO at 100 concurrency;
        - flushes + fsyncs so a hard kill leaves only fully-written lines
          on disk (every reader can JSON-decode every surviving line).
        """
        payload = self._build_result_payload(result)
        line = json.dumps(payload, ensure_ascii=False) + "\n"

        if self._save_lock is None:
            self._save_lock = asyncio.Lock()

        async with self._save_lock:
            await asyncio.to_thread(self._append_line_sync, line)

    def _append_line_sync(self, line: str) -> None:
        """Synchronous worker for _save_result; do not call from the loop."""
        with open(self.results_file, 'a', encoding='utf-8') as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def run(self) -> RolloutSummary:
        """Run pipeline (sync wrapper)"""
        return asyncio.run(self.run_async())


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Rollout Pipeline - Run agents on benchmarks")
    
    parser.add_argument("--config", type=str, required=True,
                       help="Configuration file path (.json or .yaml)")
    parser.add_argument("--data", type=str, default=None,
                       help="Benchmark data file path (overrides config)")
    parser.add_argument("--output-dir", type=str, default=None,
                       help="Output directory (overrides config)")
    parser.add_argument("--model", type=str, default=None,
                       help="Model name (overrides config)")
    parser.add_argument("--max-tasks", type=int, default=None,
                       help="Maximum number of tasks to run")
    parser.add_argument("--task-ids", type=str, nargs="+", default=None,
                       help="Specific task IDs to run")
    parser.add_argument("--no-eval", action="store_true",
                       help="Skip evaluation")
    parser.add_argument("--parallel", action="store_true",
                       help="Run tasks in parallel")
    parser.add_argument("--max-workers", type=int, default=None,
                       help="Maximum parallel workers")
    parser.add_argument("--metric", type=str, default=None,
                       choices=["exact_match", "f1_score", "contains_answer", "numeric_match", "llm_judgement", "DocBench_LasJ", "MMLongBench-Doc_LasJ", "MMLongBench-Doc_F1", "MMLongBench-Doc_Acc"],
                       help="Evaluation metric (overrides config)")
    
    args = parser.parse_args()
    
    # Load configuration
    print(f"Loading configuration: {args.config}")
    if args.config.endswith('.json'):
        config = RolloutConfig.from_json(args.config)
    elif args.config.endswith('.yaml') or args.config.endswith('.yml'):
        config = RolloutConfig.from_yaml(args.config)
    else:
        raise ValueError("Configuration file must be .json or .yaml format")
    
    # Apply overrides
    if args.data:
        config.data_path = args.data
    if args.model:
        config.model_name = args.model
    if args.max_tasks:
        config.number_of_tasks = args.max_tasks
    if args.task_ids:
        config.task_ids = args.task_ids
    if args.no_eval:
        config.evaluate_results = False
    if args.parallel:
        config.parallel = True
    if args.max_workers:
        config.max_workers = args.max_workers
    if args.metric:
        config.evaluation_metric = args.metric
    
    # Determine output directory
    output_dir = args.output_dir or config.output_dir or "rollout_results"
    
    print(f"Output directory: {output_dir}")
    
    # Run pipeline
    pipeline = RolloutPipeline(config=config, output_dir=output_dir)
    
    try:
        summary = pipeline.run()
        
        print(f"\n🏁 Final Summary:")
        for key, value in summary.to_dict().items():
            print(f"   {key}: {value}")
        
    except Exception as e:
        print(f"❌ Run failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
