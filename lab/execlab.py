"""Executor-stage edits (v18 coverage-minimized TPC-DS check). Truth: running test_execute_tpcds."""
import ast, json, os, random, subprocess, sys, time, resource
sys.path.insert(0, "/home/claude/lab"); import flowlab as F, mutlab as M
import importlib.util
spec = importlib.util.spec_from_file_location("sel18", "/tmp/app10/selector.py"); SEL = importlib.util.module_from_spec(spec); spec.loader.exec_module(SEL)
sel = SEL.Selector(); msgs = []; SEL.log = lambda *a: msgs.append(" ".join(map(str, a)))
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1])
other = sel.tpcds["other"]
files = [f for f in other if f in F.m["files"]]
rows = []; tries = 0
while len(rows) < N and tries < 5000:
    tries += 1
    rel = rng.choice(files); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    try: ast.parse("\n".join(lines))
    except SyntaxError: continue
    open(path, "w").write("\n".join(lines))
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, _ = sel.select(patch)
    force = SEL.tpcds_checkable(sel, SEL.parse_patch(patch)) if 1106 in picked else None
    if not force:                                    # only executor-stage edits that v18 now checks
        open(path, "w").write(old); continue
    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    msgs.clear()
    try: dropped = SEL.tpcds_unaffected(sel, patch)
    except Exception: dropped = False
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime)
    fails = F.run([F.ids[1106]]); open(path, "w").write(old)
    failed = fails is None or 1106 in fails
    row = {"rel": rel, "force": len(force), "dropped": dropped, "failed": failed, "miss": dropped and failed, "cpu": round(cpu, 2),
           "why": [m for m in msgs if "result check" in m][-1:] , "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2]}
    rows.append(row); print(json.dumps(row), flush=True)
