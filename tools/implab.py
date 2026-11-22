"""Mutants on function lines that also run at import. When the import-state check passes, compare our
selection with the full suite's failures (truth)."""
import json, os, random, re, subprocess, sys, ast
sys.path.insert(0, "/home/claude/work/tools"); sys.path.insert(0, "/tmp/appsim")
os.environ["BR_DATA"] = "/home/claude/work/syn"
import selector as S, codelab_mut as M
S.log = lambda *a: None
sel = S.Selector(); SG = "/home/claude/work/sg"
T = json.load(open("/tmp/appsim/tests.json"))["tests"]; NUM = {t["id"]: t["number"] for t in T}; ID = {v: k for k, v in NUM.items()}
ms = {t["number"]: round(t["seconds"] * 1000) for t in T}; S.MS_ALL.update(ms)
rng = random.Random(int(sys.argv[1])); N = int(sys.argv[2])
cands = []
for f, info in sel.files.items():
    I = set(info.get("import_lines", [])); fl = set()
    for s_, e_, sc in info["spans"]:
        if sc != "m": fl.update(range(s_, e_ + 1))
    for ln in sorted(I & fl & set(int(x) for x in info["lines"])): cands.append((f, ln))
print(len(cands), "candidate lines", flush=True)
def mutate_line(src, ln):
    lines = src.split("\n"); l = lines[ln - 1]
    tree = ast.parse(src)
    nodes = [n for n in ast.walk(tree) if getattr(n, "lineno", None) == ln and getattr(n, "end_lineno", None) == ln]
    rng.shuffle(nodes)
    for n in nodes:
        seg = l[n.col_offset:n.end_col_offset]; new = None
        if isinstance(n, ast.Constant) and type(n.value) is int and seg.isdigit(): new = str(n.value + 1)
        elif isinstance(n, ast.Constant) and isinstance(n.value, bool): new = repr(not n.value)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and 2 < len(seg) < 40 and seg[0] in "\"'" and "\\" not in seg:
            new = seg[:-1] + "x" + seg[-1]
        elif isinstance(n, ast.Compare) and len(n.ops) == 1:
            mid = l[n.left.end_col_offset:n.comparators[0].col_offset].strip() if n.left.end_lineno == ln else ""
            sw = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "is": "is not", "is not": "is", "in": "not in", "not in": "in"}
            if mid in sw: new = l[n.col_offset:n.left.end_col_offset] + " " + sw[mid] + " " + l[n.comparators[0].col_offset:n.end_col_offset]
        elif isinstance(n, ast.BoolOp):
            new = seg.replace(" and ", " or ", 1) if " and " in seg else seg.replace(" or ", " and ", 1) if " or " in seg else None
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and seg.startswith("not "): new = seg[4:]
        if not new or new == seg: continue
        lines[ln - 1] = l[:n.col_offset] + new + l[n.end_col_offset:]
        out = "\n".join(lines)
        try: ast.parse(out); return out
        except SyntaxError: lines[ln - 1] = l
    return None
done = 0
while done < N:
    rel, ln = rng.choice(cands); p = os.path.join(SG, rel); old = open(p).read()
    new = mutate_line(old, ln)
    if not new: continue
    open(p, "w").write(new); patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout
    picked, why = sel.select(patch)
    state_same = sel._state_same
    picked = S.aggressive(sel, set(picked), [rel], NUM, patch) | (set(picked) & sel.protect)
    if not state_same:
        open(p, "w").write(old); print(json.dumps({"rel": rel, "ln": ln, "state": "changed", "picked": len(picked)}), flush=True); done += 1; continue
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "-n", "2", "tests"], cwd=SG, capture_output=True, text=True, timeout=1800)
    open(p, "w").write(old)
    truth = set()
    for l in re.findall(r"^(?:FAILED|ERROR|SUBFAILED).*?tests/\S+::\S+", r.stdout, re.M | re.S):
        m = re.search(r"(tests/\S+::\S+)", l)
        if m: truth.add(NUM.get(re.sub(r"\[.*$", "", m.group(1)), -1))
    tail = r.stdout.strip().split("\n")[-1]
    miss = truth - picked; done += 1
    print(json.dumps({"rel": rel, "ln": ln, "state": "same", "diff": [x for x in patch.split("\n") if x[:1] == "+" and x[:3] != "+++"][0][:80],
                      "picked": len(picked), "cost": S.commit_cost(picked, ms), "failed": len(truth), "oracle": S.commit_cost({x for x in truth if x >= 0}, ms),
                      "MISS": sorted(ID.get(x, str(x)) for x in miss)[:4], "tail": tail[-80:]}), flush=True)
