"""Dict-value lab (testing only): change one constant inside the value of a class-body dict entry
(bool flipped, int + 1, string + "x"), keeping the keys. Only edits the dict-entry rule accepts.
Truth: the full suite. A miss = a failing test outside the fast selection."""
import ast, json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
num_of = {t["id"]: t["number"] for t in F.T}
cands = []
for rel, info in F.m["files"].items():
    if not rel.startswith("sqlglot/"): continue
    tree = ast.parse(info["text"])
    for c in ast.walk(tree):
        if not isinstance(c, ast.ClassDef): continue
        for st in c.body:
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Dict) and getattr(st.targets[0], "id", "") == "arg_types":
                for k, v in zip(st.value.keys, st.value.values):
                    if k is None: continue
                    for x in ast.walk(v):
                        if isinstance(x, ast.Constant) and type(x.value) is bool and x.lineno == x.end_lineno:
                            cands.append((rel, x.lineno, x.col_offset, x.end_col_offset, x.value)); break
print(len(cands), "candidates", file=sys.stderr)
rows = []; tries = 0
while len(rows) < N and tries < 3000:
    tries += 1
    rel, ln, a, b, val = rng.choice(cands)
    path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]; ls = old.split("\n")
    line = ls[ln - 1]
    if type(val) is bool: rep = repr(not val)
    elif type(val) is int: rep = repr(val + 1)
    else: rep = repr(val + "x")
    ls2 = list(ls); ls2[ln - 1] = line[:a] + rep + line[b:]
    new = "\n".join(ls2)
    try: ast.parse(new)
    except SyntaxError: continue
    open(path, "w").write(new)
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    why = []
    try:
        r = F.sel.dict_value_edit(rel, ast.parse(old), ast.parse(new), why)
    except Exception as e:
        r = None
    if r is None:
        open(path, "w").write(old); continue
    picked, _ = F.sel.select(patch)
    paths = [f["new"] or f["old"] for f in SEL.parse_patch(patch)]
    fast = SEL.aggressive(F.sel, set(picked), paths, num_of, patch)
    fails, tail = M.failing()
    open(path, "w").write(old)
    fails = set(fails)
    row = {"rel": rel, "ln": ln, "line": line.strip()[:90], "rep": rep, "fails": len(fails),
           "miss_fast": sorted(fails - fast), "n_fast": len(fast), "n_rule": len(r)}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
