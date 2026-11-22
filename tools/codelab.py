"""Random code mutants in sqlglot/ by operator; selection cost vs. the oracle restricted to our selection
(run exactly the selected tests: if we never miss, the failures there are all failures)."""
import ast, json, os, random, re, subprocess, sys, importlib.util, time
SG = "/home/claude/work/sg"; APP = os.environ.get("APP", "/home/claude/work/app")
os.environ["BR_DATA"] = "/home/claude/work/syn"
spec = importlib.util.spec_from_file_location("sel", APP + "/selector.py"); S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
S.log = lambda *a: None
sel = S.Selector()
T = json.load(open(APP + "/tests.json"))["tests"]; ID = {t["number"]: t["id"] for t in T}; NUM = {v: k for k, v in ID.items()}
MS = {t["number"]: round(t["seconds"] * 1000) for t in T}; num_of = NUM
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); RUN = os.environ.get("RUN", "1") == "1"
ops_want = os.environ.get("OPS", "").split(",") if os.environ.get("OPS") else None
files = [f for f in sel.files if f.startswith("sqlglot/") and len(sel.files[f]["text"]) > 2000]
def cost(nums):
    w = S.split(nums, MS); return (max(sum(MS[n] for n in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0
BIN = {ast.Add: "-", ast.Sub: "+", ast.Mult: "+", ast.FloorDiv: "/"}
def mutate(src, op):
    lines = src.split("\n"); tree = ast.parse(src)
    nodes = [n for n in ast.walk(tree) if hasattr(n, "lineno") and n.lineno == getattr(n, "end_lineno", -1)]
    rng.shuffle(nodes)
    for n in nodes:
        l = lines[n.lineno - 1]; seg = l[n.col_offset:n.end_col_offset]; new = None
        if op == "str" and isinstance(n, ast.Constant) and isinstance(n.value, str) and 2 < len(seg) < 60 and seg[0] in "\"'" and not seg.startswith(('"""', "'''")) and "\\" not in seg:
            i = rng.randrange(1, len(seg) - 1); new = seg[:i] + rng.choice(["X", "_", " "]) + seg[i:]
        elif op == "int" and isinstance(n, ast.Constant) and type(n.value) is int and seg.isdigit():
            new = str(n.value + rng.choice([1, -1]))
        elif op == "arith" and isinstance(n, ast.BinOp) and type(n.op) in BIN:
            a = l[n.left.end_col_offset:n.right.col_offset] if n.left.end_lineno == n.lineno else None
            sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//"}[type(n.op)]
            if a and a.count(sym) == 1:
                new = seg.replace(a, a.replace(sym, BIN[type(n.op)]), 1) if False else l[n.col_offset:n.left.end_col_offset] + a.replace(sym, BIN[type(n.op)]) + l[n.right.col_offset:n.end_col_offset]
        elif op == "not" and isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and seg.startswith("not "):
            new = seg[4:]
        elif op == "return_none" and isinstance(n, ast.Return) and n.value is not None and not seg.endswith(("(", "[", "{", ",")):
            new = "return None"
        elif op == "del_stmt" and isinstance(n, (ast.Expr, ast.Assign, ast.AugAssign)) and isinstance(getattr(n, "value", None), (ast.Call, ast.BinOp, ast.Name, ast.Attribute)) and seg == l.strip():
            new = "pass"
        elif op == "kwarg_bool" and isinstance(n, ast.keyword) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, bool):
            new = seg.replace(repr(n.value.value), repr(not n.value.value))
        elif op == "dict_del" and isinstance(n, ast.Dict) and False: pass
        elif op == "cmp" and isinstance(n, ast.Compare) and len(n.ops) == 1:
            sw = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.Gt: "<=", ast.LtE: ">", ast.GtE: "<", ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is"}
            mid = l[n.left.end_col_offset:n.comparators[0].col_offset] if n.left.end_lineno == n.lineno else ""
            o = mid.strip()
            if o in ("==", "!=", "<", ">", "<=", ">=", "in", "not in", "is", "is not"):
                new = l[n.col_offset:n.left.end_col_offset] + " " + sw[type(n.ops[0])] + " " + l[n.comparators[0].col_offset:n.end_col_offset]
        elif op == "method" and isinstance(n, ast.Attribute) and n.attr in ("upper", "lower", "lstrip", "rstrip", "startswith", "endswith", "append", "extend"):
            alt = {"upper": "lower", "lower": "upper", "lstrip": "rstrip", "rstrip": "lstrip", "startswith": "endswith", "endswith": "startswith", "append": "extend", "extend": "append"}
            if seg.endswith("." + n.attr): new = seg[: -len(n.attr)] + alt[n.attr]
        if new is None or new == seg: continue
        lines[n.lineno - 1] = l[:n.col_offset] + new + l[n.end_col_offset:]
        out = "\n".join(lines)
        try: ast.parse(out)
        except SyntaxError: lines[n.lineno - 1] = l; continue
        return out
    return None
OPS = ops_want or ["str", "int", "arith", "not", "return_none", "del_stmt", "kwarg_bool", "cmp", "method"]
rows = []
while len(rows) < N:
    op = OPS[len(rows) % len(OPS)]
    rel = rng.choice(files); p = os.path.join(SG, rel); old = open(p).read()
    new = mutate(old, op)
    if not new: continue
    open(p, "w").write(new)
    patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout
    t0 = time.process_time()
    picked, why = sel.select(patch)
    paths = [f["new"] or f["old"] for f in S.parse_patch(patch)]
    picked = S.aggressive(sel, set(picked), paths, num_of, patch) | (set(picked) & sel.protect)
    ours = cost(picked)
    failed = set()
    if RUN and picked:
        ids = sorted(ID[n] for n in picked)
        argf = "/tmp/codelab_ids.txt"; open(argf, "w").write("\n".join(ids))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE"] + (["-n", "2"] if len(ids) > 20 else []) + ids,
                           cwd=SG, capture_output=True, text=True, timeout=1200)
        for l in re.findall(r"^(?:FAILED|ERROR|SUBFAILED).*?tests/\S+::\S+", r.stdout, re.M | re.S):
            m = re.search(r"(tests/\S+::\S+)", l) if l.startswith(("FAILED", "ERROR", "SUBFAILED")) else None
            if m: failed.add(NUM.get(re.sub(r"\[.*$", "", m.group(1)), -1))
        if "error" in r.stdout[-300:].lower() and not failed: failed = {-2}
    open(p, "w").write(old)
    orc = cost({x for x in failed if x >= 0})
    row = {"op": op, "rel": rel, "diff": [l.strip()[:90] for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "picked": len(picked), "failed": len(failed), "ours": ours, "oracle": orc, "why": " ; ".join(why)[-150:]}
    rows.append(row); print(json.dumps(row), flush=True)
