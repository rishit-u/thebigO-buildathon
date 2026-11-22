"""Module/class-level edits in files with no executor code, where the v17.3 TPC-DS check now applies.
Truth: running test_execute_tpcds. Miss = dropped but the test fails."""
import ast, json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab"); import flowlab as F, mutlab as M
import importlib.util
spec = importlib.util.spec_from_file_location("sel173", "/tmp/app10/selector.py"); SEL = importlib.util.module_from_spec(spec); spec.loader.exec_module(SEL)
sel = SEL.Selector(); SEL.log = lambda *a: None
num_of = {t["id"]: t["number"] for t in F.T}
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1])
other = sel.tpcds["other"]
files = [f for f, info in F.m["files"].items() if f.startswith("sqlglot/") and f not in other
         and any(1106 in F.m["sets"][k] for k in info["lines"].values())]
rows = []; tries = 0
while len(rows) < N and tries < 5000:
    tries += 1
    rel = rng.choice(files); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    new = "\n".join(lines); open(path, "w").write(new)
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    files_p = SEL.parse_patch(patch)
    # module/class level only: no changed line inside a function
    t = ast.parse(old); fns = [n for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    rem, ins = SEL.changed_old_lines(files_p[0]["hunks"])
    if any(fn.body[0].lineno <= ln <= fn.end_lineno for fn in fns for ln in rem | ins):
        open(path, "w").write(old); continue
    picked, _ = sel.select(patch)
    if 1106 not in picked or not SEL.tpcds_checkable(sel, files_p):
        open(path, "w").write(old); continue
    try: dropped = SEL.tpcds_unaffected(sel, patch)
    except Exception: dropped = False
    fails = F.run([F.ids[1106]]); open(path, "w").write(old)
    failed = fails is None or 1106 in fails
    row = {"rel": rel, "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "dropped": dropped, "failed": failed, "miss": dropped and failed}
    rows.append(row); print(json.dumps(row), flush=True)
