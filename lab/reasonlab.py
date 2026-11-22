"""Local reason counter (testing only): on synthetic edits that keep TPC-DS, why does the v17.2 check keep it?"""
import ast, json, os, random, subprocess, sys, collections
sys.path.insert(0, "/home/claude/lab"); import flowlab as F, mutlab as M, scalarlab_lib as S, heldout2 as H
import importlib.util
spec = importlib.util.spec_from_file_location("sel172", "/tmp/app10/selector.py"); SEL = importlib.util.module_from_spec(spec); spec.loader.exec_module(SEL)
sel = SEL.Selector()
msgs = []
SEL.log = lambda *a: msgs.append(" ".join(map(str, a)))
num_of = {t["id"]: t["number"] for t in F.T}
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1])
pyfiles = [f for f in F.m["files"] if f.startswith("sqlglot/") and not f.endswith("__init__.py")]
rows = []; c = collections.Counter()
while len(rows) < N:
    kind = rng.choice(["body", "body", "scalar", "lambda", "swap", "bool", "flow"])
    rel = rng.choice(pyfiles); info = F.m["files"][rel]; old = info["text"]; path = os.path.join(F.TREE, rel)
    if kind == "body":
        open(path, "w").write(old); k, lines = M.mutate_py(rng, path); new = "\n".join(lines) if lines and k == "code" else None
    elif kind == "flow": r = F.mutate(rng, old); new = r[1] if r else None
    elif kind == "lambda": new = S.lambda_edit(rng, info, old)
    elif kind == "scalar": new = S.scalar_edit(rng, info, old)
    elif kind == "swap": new = H.swap_edit(rng, info, old)
    else: new = H.bool_edit(rng, info, old)
    if not new or new == old: continue
    try: ast.parse(new)
    except SyntaxError: continue
    open(path, "w").write(new)
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    open(path, "w").write(old)
    picked, why = sel.select(patch); paths = [f["new"] or f["old"] for f in SEL.parse_patch(patch)]
    msgs.clear()
    after = SEL.aggressive(sel, set(picked), paths, num_of, patch)
    if 1106 not in picked: continue
    if 1106 not in SEL.aggressive(sel, set(picked), paths, num_of, None): continue   # area rule already drops it
    m = [x for x in msgs if "TPC-DS result check" in x]
    reason = "dropped" if 1106 not in after else (m[-1].split("'why': ")[-1][:40] if m else "not checkable")
    c[reason.split(":")[-1].strip(" '}") if "query" in reason else reason] += 1
    rows.append({"rel": rel, "kind": kind, "reason": reason}); print(json.dumps(rows[-1]), flush=True)
print(dict(c))
