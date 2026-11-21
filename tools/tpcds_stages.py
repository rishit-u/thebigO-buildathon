"""Which sqlglot lines test_execute_tpcds runs OUTSIDE the fingerprinted stages (mechanism 1A).

Replays the test's own steps in one process under coverage with two contexts:
  fp    : parse_one, pickle round trip, transpile(write="duckdb"), optimize, Plan
  other : everything else (setUpClass, find_tables, ensure_schema/ensure_tables, PythonExecutor,
          the Python generator, result comparison)
Writes {file: [lines run in "other"]}. Run from the pinned sqlglot checkout (needs duckdb, pandas).

Usage: python tools/tpcds_stages.py <out.json>
"""
import json, os, pickle, sys
import coverage

sys.path.insert(0, os.getcwd())
# import every sqlglot module first: import-time lines are handled by the selector's own rules
import importlib, pkgutil
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass
from sqlglot import parse_one, transpile, find_tables
import sqlglot.executor as ex
from tests import test_executor as te
cov = coverage.Coverage(source=["sqlglot"], data_file=None)
cov.start(); cov.switch_context("other")

T = te.TestExecutor
T.setUpClass()
real_opt, real_plan = ex.optimize, ex.Plan
def fp_stage(f):
    def w(*a, **k):
        cov.switch_context("fp")
        try: return f(*a, **k)
        finally: cov.switch_context("other")
    return w
ex.optimize, ex.Plan = fp_stage(real_opt), fp_stage(real_plan)
te.initializer(te.TPCDS_SCHEMA, T.tpcds_tables)
case = T("test_execute_tpcds")
for i, (meta, sql, _) in enumerate(T.tpcds_sqls):
    e = fp_stage(parse_one)(sql)
    e = fp_stage(lambda x: pickle.loads(pickle.dumps(x)))(e)
    table = te.mp_execute(e, meta)                     # optimize/Plan inside run as "fp"
    if table is not None:
        duck = fp_stage(lambda s: transpile(s, write="duckdb")[0])(sql)
        T.cache[sql] = T.tpcds_conn.execute(duck).fetchdf()   # what cached_execute does, split by stage
        case.subtestHelper(i, table, tpch=False)        # the comparison itself is "other"
cov.stop()
data = cov.get_data()
out = {}
for f in data.measured_files():
    rel = os.path.relpath(f, os.getcwd())
    if not rel.startswith("sqlglot/"): continue
    lines = set()
    for ln, ctxs in data.contexts_by_lineno(f).items():
        if "other" in ctxs: lines.add(ln)
    if lines: out[rel] = sorted(lines)
json.dump(out, open(sys.argv[1], "w"))
print(len(out), "files run outside the fingerprinted stages", file=sys.stderr)
