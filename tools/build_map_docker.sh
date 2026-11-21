#!/usr/bin/env bash
# Rebuild map.json.gz inside the organizers' exact image (same Python and SQLite they
# labeled with), so the coverage map matches their environment.
#
# Run from the repository root:   bash tools/build_map_docker.sh
# Needs Docker and git. Takes about 5-10 minutes (longer on Apple Silicon, which emulates amd64).
set -euo pipefail

IMAGE="python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
PIN="eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
WORK="$(pwd)/.mapbuild"

mkdir -p "$WORK"
if [ ! -d "$WORK/sqlglot/.git" ]; then
  git clone --quiet https://github.com/tobymao/sqlglot "$WORK/sqlglot"
fi
git -C "$WORK/sqlglot" checkout --quiet "$PIN"
rm -rf "$WORK/reads" "$WORK/sqlglot/.coverage"* && mkdir -p "$WORK/reads"
cp map.json.gz "$WORK/map_before.json.gz" 2>/dev/null || true

docker run --rm --platform linux/amd64 \
  -v "$(pwd):/repo" -v "$WORK:/work" -w /work/sqlglot \
  -e SKIP_INTEGRATION=0 -e BR_READS_DIR=/work/reads -e PYTHONPATH=/work/sqlglot:/repo/tools \
  "$IMAGE" sh -c '
    set -e
    pip install -q -r /repo/requirements.lock coverage==7.17.4 pytest-cov==7.1.0
    python -c "import sqlite3; print(\"sqlite\", sqlite3.sqlite_version)"
    python -m pytest tests -q -p no:cacheprovider -p brplugin -n 2 \
      --cov=sqlglot --cov-context=test --cov-report= | tail -3
    python /repo/tools/build_map.py /work/sqlglot /repo/sample/tests.json /work/reads /work/map.json
    gzip -9f /work/map.json
  '

cp "$WORK/map.json.gz" map.json.gz
echo "New map written to map.json.gz (old one kept at $WORK/map_before.json.gz)."
echo "Expected above: a sqlite version below 3.49 and '1620 passed, 1 skipped'."
echo "Next: run 'make sample' and compare with the previous score (184.649 s, 0 misses)."
