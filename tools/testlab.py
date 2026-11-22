"""Random edits inside test modules; truth = pytest on the module and its importers. Miss = a failing
test not in the selection."""
import ast, json, os, random, re, subprocess, sys, importlib.util
SG = "/home/claude/work/sg"; APP = "/home/claude/work/app"
os.environ["BR_DATA"] = "/home/claude/work/syn"
spec = importlib.util.spec_from_file_location("sel", APP + "/selector.py"); S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
S.log = lambda *a: None
sel = S.Selector()
T = json.load(open(APP + "/tests.json"))["tests"]; ID = {t["number"]: t["id"] for t in T}; NUM = {v: k for k, v in ID.items()}
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); maxmod = int(sys.argv[3]) if len(sys.argv) > 3 else 400
files = [f for f in sel.test_files if os.path.exists(os.path.join(SG, f)) and len(sel.sets[sel.test_files[f]]) <= maxmod]
if os.environ.get("ONLY"): files = os.environ["ONLY"].split(",")
def mutate(src):
    lines = src.split("\n"); tree = ast.parse(src)
    cands = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and n.lineno == n.end_lineno:
            cands.append(n)
        elif isinstance(n, ast.Compare) and n.lineno == n.end_lineno: cands.append(n)
    rng.shuffle(cands)
    for n in cands[:20]:
        l = lines[n.lineno - 1]; seg = l[n.col_offset:n.end_col_offset]
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool): new = repr(not v)
            elif isinstance(v, int): new = repr(v + rng.choice([1, -1]))
            elif isinstance(v, str) and len(v) > 2 and not seg.startswith(('"""', "'''")) and '\\' not in seg:
                i = rng.randrange(1, len(seg) - 1); new = seg[:i] + rng.choice(["x", " ", "1", ""]) + seg[i:]
            else: continue
        else:
            ops = {"==": "!=", "!=": "==", "<": "<=", ">": ">=", " in ": " not in ", " is ": " is not "}
            k = next((o for o in ops if o in seg), None)
            if not k: continue
            new = seg.replace(k, ops[k], 1)
        lines[n.lineno - 1] = l[:n.col_offset] + new + l[n.end_col_offset:]
        out = "\n".join(lines)
        try: ast.parse(out)
        except SyntaxError: continue
        return out
    # fallback: delete a random statement line inside a function
    return None
rows = []
while len(rows) < N:
    rel = rng.choice(files); p = os.path.join(SG, rel); old = open(p).read()
    new = mutate(old)
    if not new or new == old: continue
    open(p, "w").write(new)
    patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout
    picked, why = sel.select(patch)
    modset = set(sel.sets[sel.test_files[rel]])
    mfiles = sorted({ID[n].split("::")[0] for n in modset})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "-n", "2"] + mfiles,
                       cwd=SG, capture_output=True, text=True, timeout=900)
    open(p, "w").write(old)
    failed = set()
    for l in r.stdout.split("\n"):
        m = re.search(r"(tests/\S+::\S+)", l) if l.startswith(("FAILED", "ERROR", "SUBFAILED")) else None
        if m:
            tid = m.group(1)
            tid = re.sub(r"\[.*$", "", tid) if tid not in NUM else tid
            failed.add(NUM.get(tid, -1))
    miss = failed - picked
    row = {"rel": rel, "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "picked": len(picked), "failed": len(failed), "miss": sorted(ID.get(m, str(m)) for m in miss)[:3], "why": why[-1][:90]}
    rows.append(row); print(json.dumps(row), flush=True)
print("MISSES", sum(1 for r in rows if r["miss"]), "of", len(rows))
