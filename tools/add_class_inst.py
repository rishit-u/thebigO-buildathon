"""Add class-level reach to map.json: for every class, the tests that created an object with that
class in its MRO (from instq-*.json written by brplugin), and whether its attributes are read while
importing by code outside its own subclasses (then class-level reach is not enough).

Usage: python tools/add_class_inst.py <map.json in> <tests.json> <reads dir> <map.json out>
Used only by aggressive mode in selector.py.
"""
import ast, glob, json, os, sys

src_map, tests_json, reads_dir, out = sys.argv[1:5]
M = json.load(open(src_map))
T = json.load(open(tests_json))
num = {t["id"]: t["number"] for t in T["tests"]}
by_class = {}
for t in T["tests"]:
    by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
sets = M["sets"]; set_ids = {tuple(s): i for i, s in enumerate(sets)}
def sid(nums):
    key = tuple(sorted(set(nums)))
    if key not in set_ids: set_ids[key] = len(sets); sets.append(list(key))
    return set_ids[key]
def ctx_tests(c):
    nodeid, _, phase = c.partition("|")
    if nodeid.startswith("<class>"): return by_class.get(nodeid[len("<class>"):], set())
    if phase in ("setup", "teardown"): return by_class.get(nodeid.rsplit("::", 1)[0], set())
    return {num[nodeid]} if nodeid in num else set()

inst = {}          # "file::qualname" -> tests
at_import = set()  # created outside any test (import / collection): everyone may depend on it
files_seen = glob.glob(os.path.join(reads_dir, "instq-*.json"))
assert files_seen, "no instq-*.json files"
for f in files_seen:
    for ctx, quals in json.load(open(f)).items():
        if ctx.startswith("<") and not ctx.startswith("<class>"):
            at_import |= set(quals); continue
        ts = ctx_tests(ctx)
        for q in quals: inst.setdefault(q, set()).update(ts)

def chain(n):
    parts = []
    while isinstance(n, ast.Attribute): parts.append(n.attr); n = n.value
    if isinstance(n, ast.Name): parts.append(n.id); return list(reversed(parts))
    return None

# Method names anywhere in sqlglot: reading a method off a class at import (exp.Foo.from_arg_list)
# only binds it; calls happen at run time and are seen by coverage.
methods, assigned = set(), set()
trees = {rel: ast.parse(info["text"]) for rel, info in M["files"].items()}
for tree in trees.values():
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef):
            for st in c.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)): methods.add(st.name)
                elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                    for tg in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                        if isinstance(tg, ast.Name): assigned.add(tg.id)
harmless = methods - assigned

# Import-time attribute reads: chains read in module- or class-level code (not inside functions),
# with the dotted bases of the class body they are in (reads of a direct base are covered by the MRO).
refs = []   # (components, exempt prefixes)
def visit(body, bases_txt):
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in st.decorator_list + st.args.defaults + [x for x in st.args.kw_defaults if x]:
                scan(d, bases_txt)
            continue
        if isinstance(st, ast.ClassDef):
            for d in st.decorator_list + st.bases + [k.value for k in st.keywords]: scan(d, bases_txt)
            visit(st.body, {".".join(c) for c in map(chain, st.bases) if c}); continue
        for fld, val in ast.iter_fields(st):
            if fld in ("body", "orelse", "finalbody", "handlers") and isinstance(val, list) and val and isinstance(val[0], ast.AST) and not isinstance(val[0], ast.expr):
                if fld == "handlers":
                    for h in val: visit(h.body, bases_txt)
                else: visit(val, bases_txt)
            elif isinstance(val, ast.AST): scan(val, bases_txt)
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, ast.AST): scan(v, bases_txt)
def scan(node, bases_txt):
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            c = chain(n)
            if c and len(c) >= 2 and c[-1] not in harmless: refs.append((c, bases_txt))
        if isinstance(n, ast.Lambda): pass   # lambda bodies run later; ast.walk still enters them (conservative)
for tree in trees.values(): visit(tree.body, set())

def copied(qual):
    q = qual.split(".")
    for c, bases_txt in refs:
        for i in range(len(c) - len(q)):   # q must be followed by at least one more attribute
            if c[i:i + len(q)] == q:
                prefix = ".".join(c[: i + len(q)])
                if prefix not in bases_txt: return True
    return False

# Run-time references to a class by name (Foo.X, exp.Foo.from_arg_list(...), isinstance(x, Foo)):
# tests that ran any line inside a function that mentions the name. Same-named classes share it.
name_lines = {}
for rel, tree in trees.items():
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): continue
        for st in ast.walk(fn):
            if not isinstance(st, ast.stmt) and st is not fn: continue
            heads = [st.body] if isinstance(st, ast.Lambda) else [v for f_, v in ast.iter_fields(st)
                     if f_ not in ("body", "orelse", "finalbody", "handlers")]
            for h in heads:   # the statement's own expressions; nested statements are visited themselves
                for x in (h if isinstance(h, list) else [h]):
                    if not isinstance(x, ast.AST): continue
                    for n in ast.walk(x):
                        nm = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
                        if nm and nm[:1].isupper():   # credited to every line of the statement
                            for ln in range(st.lineno, (st.end_lineno or st.lineno) + 1):
                                name_lines.setdefault(nm, set()).add((rel, ln))
ref_cache = {}
def ref_tests(name):
    if name not in ref_cache:
        ts = set()
        for rel, ln in name_lines.get(name, ()):
            k = M["files"][rel]["lines"].get(str(ln))
            if k is not None: ts.update(sets[k])
        ref_cache[name] = ts
    return ref_cache[name]

n_cls = n_inst = 0
for rel, tree in trees.items():
    entries = []
    def walk(body, outer):
        global n_cls, n_inst
        for st in body:
            if isinstance(st, ast.ClassDef):
                qual = (outer + "." if outer else "") + st.name; key = f"{rel}::{qual}"
                n_cls += 1
                ts = inst.get(key)
                if ts is None or key in at_import or copied(qual) or st.decorator_list: s = -1
                else: s = sid(ts | ref_tests(qual.split(".")[0])); n_inst += 1   # nested: via the outer class
                entries.append([qual, st.lineno, st.end_lineno, s])
                walk(st.body, qual)
    walk(tree.body, "")
    M["files"][rel]["classes"] = entries
json.dump(M, open(out, "w"), separators=(",", ":"))
print(f"classes {n_cls}, with class-level reach {n_inst}", file=sys.stderr)
