import json, os, random, subprocess, sys, importlib.util, resource, re
sys.path.insert(0, "/home/claude/work/tools")
os.environ["BR_DATA"] = "/home/claude/work/syn"
spec = importlib.util.spec_from_file_location("sel", "/tmp/appsim/selector.py"); S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
logs = []; S.log = lambda *a: logs.append(" ".join(map(str, a)))
sel = S.Selector(); SG = "/home/claude/work/sg"
T = json.load(open("/tmp/appsim/tests.json"))["tests"]; NUM = {t["id"]: t["number"] for t in T}
import codelab_mut as M
rng = random.Random(int(sys.argv[1]))
files = [f for f in sel.files if f.startswith(("sqlglot/optimizer/", "sqlglot/executor/", "sqlglot/planner.py", "sqlglot/generators/duckdb.py", "sqlglot/generators/python.py"))]
def ch(): u = resource.getrusage(resource.RUSAGE_CHILDREN); return u.ru_utime + u.ru_stime
n = 0
while n < int(sys.argv[2]):
    rel = rng.choice(files); p = os.path.join(SG, rel); old = open(p).read()
    new = M.mutate(old, rng.choice(["str", "int", "cmp", "not", "kwarg_bool", "del_stmt"]), rng)
    if not new: continue
    open(p, "w").write(new); patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout; open(p, "w").write(old)
    picked, why = sel.select(patch)
    if NUM[S.TPCDS_ID] not in picked: continue
    logs.clear(); c0 = ch()
    out = S.aggressive(sel, set(picked), [rel], NUM, patch)
    n += 1
    print(f"{rel:45} cpu={ch()-c0:5.2f} tpcds={'kept' if NUM[S.TPCDS_ID] in out else 'dropped'} | {' / '.join(l for l in logs if 'TPC-DS' in l)[:150]}", flush=True)
