#!/usr/bin/env bash
# Mini smoke runner. Picks one of the sample configs in
# configs/infer/*.parallel.json, runs against the *real* sandbox server
# and the *real* LLM endpoint described by OPENAI_API_KEY/OPENAI_BASE_URL,
# and walks the results jsonl + summary to assert basic sanity.
#
# Usage:
#   bash run_smoke.sh rag    # use rag_infer.parallel.json
#   bash run_smoke.sh web    # use web_infer.parallel.json
#   bash run_smoke.sh gui    # use gui_infer.parallel.json
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
SCENARIO="${1:-rag}"

CONFIG="$REPO_ROOT/configs/infer/${SCENARIO}_infer.parallel.json"
if [[ ! -f "$CONFIG" ]]; then
    echo "[L3] config not found: $CONFIG" >&2
    exit 2
fi

# Sanity check: sandbox server is up.
SANDBOX_URL="${SANDBOX_URL:-http://127.0.0.1:18890}"
case "$SCENARIO" in
    web) SANDBOX_CONFIG_HINT="configs/sandbox-server/web_config.json" ;;
    gui) SANDBOX_CONFIG_HINT="configs/sandbox-server/GUI_config.json" ;;
    *) SANDBOX_CONFIG_HINT="configs/sandbox-server/rag_config.json" ;;
esac
if ! curl -sf "$SANDBOX_URL/health" >/dev/null 2>&1; then
    echo "[L3] sandbox server not reachable at $SANDBOX_URL/health" >&2
    echo "[L3] hint: bash $REPO_ROOT/start_sandbox_server.sh --config $SANDBOX_CONFIG_HINT" >&2
    exit 3
fi

# Sanity check: LLM creds set.
if [[ -z "${OPENAI_API_KEY:-}" || -z "${OPENAI_BASE_URL:-}" ]]; then
    echo "[L3] OPENAI_API_KEY / OPENAI_BASE_URL must be set" >&2
    exit 4
fi

OUT_DIR="$HERE/.last_smoke_${SCENARIO}"
mkdir -p "$OUT_DIR"

PY="${PYTHON:-python}"

# Keep the mini-run small even when the sample config is tuned for high concurrency.
cd "$REPO_ROOT"
$PY -m rollout.pipeline \
    --config "$CONFIG" \
    --api-key "$OPENAI_API_KEY" \
    --base-url "$OPENAI_BASE_URL" \
    --output-dir "$OUT_DIR" \
    --max-tasks 3 \
    --no-eval \
    --parallel \
    --max-workers 2 \
    2>&1 | tee "$OUT_DIR/smoke.log"

# Sanity assertions on the produced artifacts.
$PY - "$OUT_DIR" <<'PYEOF'
import json
import os
import sys

out_dir = sys.argv[1]
results = [f for f in os.listdir(out_dir) if f.startswith("results_") and f.endswith(".jsonl")]
assert results, f"no results_*.jsonl in {out_dir}"
rows = []
with open(os.path.join(out_dir, results[0]), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
assert len(rows) >= 1, "0 rows in results"
ok = sum(1 for r in rows if r.get("success"))
print(f"[L3] rows={len(rows)} ok={ok} fail={len(rows)-ok}")
assert ok >= 1, "no successful task; check sandbox / LLM connectivity"

summary_files = [f for f in os.listdir(out_dir) if f.startswith("summary_")]
if summary_files:
    summary = json.load(open(os.path.join(out_dir, summary_files[0]), encoding="utf-8"))
    print(f"[L3] tool_stats={summary.get('tool_stats')}")
PYEOF

echo "[L3] OK (artifacts in $OUT_DIR)"
