"""Class-body lab (testing only): delete one single-line entry of a dict/set/list/tuple assigned in a
class body. Ground truth: the full suite. Reports misses for aggressive (class reach) and safe mode."""
import ast, json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
num_of = {t["id"]: t["number"] for t in F.T}
cands = []
for rel, info in F.m["files"].items():
    if not rel.startswith("sqlglot/"): continue
    tree = ast.parse(info["text"]); lines = info["text"].split("\n")
    for c in ast.walk(tree):
        if not isinstance(c, ast.ClassDef): continue
        for st in c.body:
            if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None: continue
            v = st.value
            elts = list(zip(v.keys, v.values)) if isinstance(v, ast.Dict) else [(e, e) for e in getattr(v, "elts", [])] if isinstance(v, (ast.Set, ast.List, ast.Tuple)) else []
            for k, val in elts:
                if k is None or k.lineno != val.end_lineno or k.lineno == st.lineno: continue
                ln = k.lineno; text = lines[ln - 1].strip()
                if text.endswith(",") and text.count(",") == 1 or text.endswith(",") and isinstance(v, ast.Dict):
                    cands.append((rel, ln))
print(len(cands), "candidates", file=sys.stderr)
rows = []
for rel, ln in rng.sample(cands, N):
    path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]; ls = old.split("\n")
    new = "\n".join(ls[: ln - 1] + ls[ln:])
    try: ast.parse(new)
    except SyntaxError: continue
    open(path, "w").write(new)
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, why = F.sel.select(patch)
    paths = [f["new"] or f["old"] for f in SEL.parse_patch(patch)]
    fast = SEL.aggressive(F.sel, set(picked), paths, num_of)
    fails, tail = M.failing()
    open(path, "w").write(old)
    fails = set(fails)
    row = {"rel": rel, "ln": ln, "line": ls[ln - 1].strip()[:80], "fails": len(fails),
           "miss_safe": sorted(fails - set(picked)), "miss_fast": sorted(fails - fast),
           "n_safe": len(picked), "n_fast": len(fast), "why": [w for w in why if "->" in w][:3]}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
