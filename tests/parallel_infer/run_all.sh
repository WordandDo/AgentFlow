#!/usr/bin/env bash
# Usage:
#   bash tests/parallel_infer/run_all.sh          # L0 + L1 + L2
#   bash tests/parallel_infer/run_all.sh L0       # only L0
#   bash tests/parallel_infer/run_all.sh L0 L1    # L0 and L1
#   bash tests/parallel_infer/run_all.sh fast     # L0 + L1 (skip L2)
set -uo pipefail

# Resolve via realpath so symlinks (e.g. /home -> /workspace) don't make
# pytest's rootdir disagree with the path it sees on the CLI; if rootdir
# and the test path point at the same files via different prefixes,
# conftest.py is not picked up and `rollout` becomes unimportable.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
LOG="$HERE/.last_run.log"
: > "$LOG"

LEVELS=("$@")
if [[ ${#LEVELS[@]} -eq 0 ]]; then
    LEVELS=(L0 L1 L2)
fi
if [[ "${LEVELS[0]:-}" == "fast" ]]; then
    LEVELS=(L0 L1)
fi

# Prefer the project's `af` conda env if PYTEST is not set in the
# caller's environment (the base env typically does not have pytest
# or the project's runtime deps installed).
if [[ -z "${PYTEST:-}" ]]; then
    if [[ -x "/home/yanguochen/miniconda3/envs/af/bin/pytest" ]]; then
        PYTEST="/home/yanguochen/miniconda3/envs/af/bin/pytest"
    else
        PYTEST="pytest"
    fi
fi
PYTEST_FLAGS=${PYTEST_FLAGS:-"-q --maxfail=5 --color=yes"}

# Use REPO-ROOT-relative paths so pytest's auto-detected rootdir always
# matches the test file's apparent path (avoids the symlink mismatch
# described above).
declare -A DIRS=(
    [L0]="tests/parallel_infer/L0_smoke"
    [L1]="tests/parallel_infer/L1_unit"
    [L2]="tests/parallel_infer/L2_integration"
)

cd "$REPO_ROOT"
overall=0
for lvl in "${LEVELS[@]}"; do
    dir="${DIRS[$lvl]:-}"
    if [[ -z "$dir" ]]; then
        echo "[SKIP] $lvl is not a pytest level (L3/L4/L5 are scripted, see their README.md)" | tee -a "$LOG"
        continue
    fi
    echo "==============================================================" | tee -a "$LOG"
    echo " RUN $lvl  ($dir)" | tee -a "$LOG"
    echo "==============================================================" | tee -a "$LOG"
    start=$(date +%s)
    # shellcheck disable=SC2086
    $PYTEST $PYTEST_FLAGS "$dir" 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}
    end=$(date +%s)
    echo "[$lvl] exit=$rc  duration=$((end-start))s" | tee -a "$LOG"
    if [[ $rc -ne 0 ]]; then
        overall=$rc
    fi
done

echo "==============================================================" | tee -a "$LOG"
echo " SUMMARY"
grep -E "^\[(L[0-9])\] exit=" "$LOG"
echo "Overall exit: $overall"
exit $overall
