"""Build map.json from the pinned sqlglot checkout. Run offline, commit the output.

Usage: python tools/build_map.py <sqlglot checkout> <tests.json> <reads dir> <out map.json>
The checkout must hold the .coverage file from:
  SKIP_INTEGRATION=0 pytest tests -p brplugin --cov=sqlglot --cov-context=test --cov-report=
"""
import ast, glob, json, os, re, sys
from coverage import CoverageData

src, tests_json, reads_dir, out = sys.argv[1:5]
T = json.load(open(tests_json))
num = {t["id"]: t["number"] for t in T["tests"]}
ALL = sorted(num.values())

sets, set_ids = [], {}
def sid(nums):
    key = tuple(sorted(set(nums)))
    if key not in set_ids:
        set_ids[key] = len(sets); sets.append(list(key))
    return set_ids[key]

by_class = {}
for t in T["tests"]:
    by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])

def ctx_tests(ctxs):
    """Coverage/read contexts -> test numbers. Setup-phase work (setUpClass etc.) is shared
    by the whole class, so it counts for every test in that class."""
    out = set()
    for c in ctxs:
        nodeid, _, phase = c.partition("|")
        if nodeid.startswith("<class>"):
            out |= by_class.get(nodeid[len("<class>"):], set())
        elif phase == "setup" or phase == "teardown":
            out |= by_class.get(nodeid.rsplit("::", 1)[0], set())
        elif nodeid in num:
            out.add(num[nodeid])
    return out

def spans(tree):
    """[start, end, scope] for every simple statement and compound-statement header.
    scope 'f' = inside a function body (runs when called), 'm' = module/class level (runs at import)."""
    res = []
    def visit(body, scope):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in st.decorator_list] + [st.lineno])
                hdr_end = st.body[0].lineno - 1 if st.body[0].lineno > st.lineno else st.lineno
                res.append([start, hdr_end, scope])
                visit(st.body, "f")
            elif isinstance(st, ast.ClassDef):
                start = min([d.lineno for d in st.decorator_list] + [st.lineno])
                res.append([start, max(st.lineno, st.body[0].lineno - 1), scope])
                visit(st.body, scope)
            elif hasattr(st, "body") and isinstance(st.body, list):  # if/for/while/with/try/match
                first = st.body[0].lineno if st.body else st.lineno
                res.append([st.lineno, max(st.lineno, first - 1), scope])
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(st, field, []) or [], scope)
                for h in getattr(st, "handlers", []) or []:
                    res.append([h.lineno, max(h.lineno, h.body[0].lineno - 1), scope]); visit(h.body, scope)
                for case in getattr(st, "cases", []) or []:
                    res.append([case.pattern.lineno, max(case.pattern.lineno, case.body[0].lineno - 1), scope]); visit(case.body, scope)
            else:
                res.append([st.lineno, st.end_lineno, scope])
    visit(tree.body, "m")
    return sorted(res)

cov = CoverageData(os.path.join(src, ".coverage")); cov.read()
files = {}
for path in sorted(cov.measured_files()):
    rel = os.path.relpath(path, src)
    text = open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    cbl = cov.contexts_by_lineno(path)
    users = set()
    lines = {}
    import_lines = sorted(ln for ln, ctxs in cbl.items() if "" in ctxs)  # ran while importing
    for ln, ctxs in cbl.items():
        ts = ctx_tests(ctxs); users |= ts
        if ts: lines[str(ln)] = sid(ts)
    files[rel] = {
        "users": sid(users),           # every test that ran any line of this file
        "lines": lines,                # line -> set id (only lines some test ran)
        "spans": spans(tree),
        # function bodies [first body line, last line]: who entered a function = who ran any line in it
        "funcs": sorted([n.body[0].lineno, n.end_lineno] for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        "import_lines": import_lines,  # lines run at import time: a change there affects everyone
        "ast": ast.dump(tree),
        "text": text,                  # pinned source, so patches can be applied at run time
    }

# Fixture map: repo files each test opened (fixtures, and explicit reads of .py source).
reads = {}
for f in glob.glob(os.path.join(reads_dir, "reads-*.json")):
    for k, v in json.load(open(f)).items():
        for p in v: reads.setdefault(p, set()).add(k)
read_map = {}
for p, ks in reads.items():
    if any(k.startswith("<") and not k.startswith("<class>") for k in ks): read_map[p] = sid(ALL)   # read at collection: affects everyone
    else: read_map[p] = sid(ctx_tests(ks))

# Instantiation map: source file -> tests that created an object whose MRO has a class from it.
inst = {}
for f in glob.glob(os.path.join(reads_dir, "inst-*.json")):
    for ctx, fs in json.load(open(f)).items():
        if ctx.startswith("<") and not ctx.startswith("<class>"):
            continue   # created while importing: covered by the import-time rule
        ts = ctx_tests([ctx])
        for p in fs: inst.setdefault(p, set()).update(ts)

# Static import graph of sqlglot (any import anywhere in a file). Package __init__ files are
# re-export hubs that everything imports, so they are not followed as importers.
modfile = {}
for rel in files:
    mod = rel[:-3].replace("/", ".")
    if mod.endswith(".__init__"): mod = mod[: -len(".__init__")]
    modfile[mod] = rel
# Top-level bindings of each file: classes and functions are tracked by the instantiation map
# and by coverage; constants (anything else) can be read silently by importers.
kinds = {}
for rel, info in files.items():
    k = {}
    for st in ast.parse(info["text"]).body:
        if isinstance(st, ast.ClassDef): k[st.name] = "class"
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)): k[st.name] = "def"
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for tg in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                for n in ast.walk(tg):
                    if isinstance(n, ast.Name): k[n.id] = "const"
    kinds[rel] = k

def silent(f, names):
    """Does importing `names` from file f bring in something only coverage/instances can't see?"""
    if names is None: return True              # the module object itself
    return any(kinds[f].get(n, "const") == "const" for n in names)

# For each import of a constant or module object: where the importer uses that name.
uses = {rel: [] for rel in files}     # f -> [(importer g, [lines in g that use it])]
def dotted(n):
    parts = []
    while isinstance(n, ast.Attribute): parts.append(n.attr); n = n.value
    if isinstance(n, ast.Name): parts.append(n.id); return ".".join(reversed(parts))
    return None
for rel, info in files.items():
    if rel.endswith("__init__.py"): continue
    pkg = rel[:-3].replace("/", ".").rsplit(".", 1)[0]
    tree = ast.parse(info["text"])
    found = []   # (file f, local name or dotted path to match)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a_ in node.names:
                m = a_.name
                while m and m not in modfile: m = m.rsplit(".", 1)[0] if "." in m else ""
                if m: found.append((modfile[m], a_.asname or a_.name))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)] + ([base] if base else []))
            for a_ in node.names:
                full = f"{base}.{a_.name}"
                if full in modfile: found.append((modfile[full], a_.asname or a_.name))
                else:
                    m = base
                    while m and m not in modfile: m = m.rsplit(".", 1)[0] if "." in m else ""
                    if m and kinds[modfile[m]].get(a_.name, "const") == "const":
                        found.append((modfile[m], a_.asname or a_.name))
    for f, local in found:
        if f == rel or f.endswith("__init__.py"): continue
        lines = sorted({n.lineno for n in ast.walk(tree)
                        if isinstance(n, (ast.Name, ast.Attribute)) and not isinstance(n, ast.Import)
                        and (dotted(n) == local or (dotted(n) or "").startswith(local + "."))
                        and isinstance(getattr(n, "ctx", None), ast.Load)})
        if lines: uses[f].append((rel, lines))

def scope_of(rel, ln):
    for s_, e_, sc in files[rel]["spans"]:
        if s_ <= ln <= e_: return (s_, e_, sc)
    return None

memo = {}
def reach(rel, stack=()):
    """Tests a module/class-level change in rel can reach."""
    if rel in memo: return memo[rel]
    if rel in stack: return set()
    ts = set(sets[files[rel]["users"]]) | inst.get(rel, set())
    if rel in reads: ts |= ctx_tests(reads[rel])
    imp_lines = set(files[rel]["import_lines"])
    for g, lines in uses[rel]:
        g_lines, g_imp = files[g]["lines"], set(files[g]["import_lines"])
        for ln in lines:
            st = scope_of(g, ln)
            if st is None or st[2] == "m" or any(x in g_imp for x in range(st[0], st[1] + 1)):
                ts |= reach(g, stack + (rel,))          # used while importing g: whoever g reaches
            else:
                for x in range(st[0], st[1] + 1):        # used at run time: whoever runs that statement
                    if str(x) in g_lines: ts |= set(sets[g_lines[str(x)]])
    memo[rel] = ts
    return ts

for rel, info in files.items():
    info["mod_users"] = sid(reach(rel))

# Test files: their own tests, plus tests in files that import them (helpers, base classes).
by_file = {}
for t in T["tests"]: by_file.setdefault(t["file"], []).append(t["number"])
test_files = {}
pyfiles = sorted(glob.glob(os.path.join(src, "tests", "**", "*.py"), recursive=True))
for p in pyfiles:
    rel = os.path.relpath(p, src); mod = rel[:-3].replace("/", ".")
    nums = set(by_file.get(rel, []))
    leaf = mod.split(".")[-1]
    pat = re.compile(rf"(from\s+{re.escape(mod)}\s+import|import\s+{re.escape(mod)}\b|from\s+\.{{1,2}}{re.escape(leaf)}\s+import|from\s+tests(\.\w+)*\s+import\s+[^\n]*\b{re.escape(leaf)}\b)")
    for q in pyfiles:
        if q != p and pat.search(open(q, encoding="utf-8").read()):
            nums |= set(by_file.get(os.path.relpath(q, src), []))
    test_files[rel] = sid(nums if nums else ALL)

json.dump({"sets": sets, "all": sid(ALL), "files": files, "reads": read_map, "test_files": test_files},
          open(out, "w"), separators=(",", ":"))
print(f"{len(files)} source files, {len(sets)} unique test sets, {len(read_map)} read files, {os.path.getsize(out)/1e6:.1f} MB", file=sys.stderr)
