# thebigO — Blast Radius selector

For each sqlglot patch, pick the tests that could fail and split them over the
cheapest number of workers. See `SCORING.md` for the exact rules.

## Run it

    make sample   # build, run on the 30 sample commits under the scorer's limits, score

## How it works (v1)

1. `map.json.gz` is built once from sqlglot at the pinned commit
   (`tools/build_map.py`): for every source line, which tests ran it; which
   fixture files each test opened; and which test files import which.
2. For each patch, `selector.py` applies it to the pinned source and:
   - syntax error -> all tests
   - same Python AST as before (comment/whitespace only) -> no tests
   - a changed line inside a function -> the tests that ran that statement
   - a changed module- or class-level line -> all tests (v1 is conservative here)
   - a fixture file -> the tests that read it; a test file -> its tests and importers
   - anything else -> all tests
3. The chosen tests are split longest-first onto the lightest worker, trying
   1..8 workers and keeping the cheapest.

## Rebuilding the map

    git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
    pip install -r requirements.lock pytest-cov
    cd sqlglot && SKIP_INTEGRATION=0 BR_READS_DIR=/tmp/reads PYTHONPATH=.:../tools \
      python -m pytest tests -p no:cacheprovider -p brplugin -n 2 --cov=sqlglot --cov-context=test --cov-report=
    cd .. && python tools/build_map.py sqlglot sample/tests.json /tmp/reads map.json && gzip -9f map.json

(mkdir /tmp/reads first.)
