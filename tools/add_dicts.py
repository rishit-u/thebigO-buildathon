"""Add per-entry reads of class-body dicts to map.json (from valprof-*.json, round 7 recordings).

For every dict display assigned in a class body (site "file:line"): which tests read the value of
each key (d[k], d.get(k)), which tests read all values (items/values/copy/|), which keys were read
while importing outside a class-body dict merge ("derived"), and flags (mutated at run time, a key
that cannot be named). Plus identity groups (entries holding the same value object, i.e. copied by a
class-body {**Parent.X} merge) and the attribute names merged at import into a dict that is NOT a
class-body dict display (those copies are not recorded, so their entries keep the old rules).

Usage: python tools/add_dicts.py <map.json in> <tests.json> <reads dir> <map.json out>
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
ALL = set(num.values())
def ctx_tests(c):
    if c.startswith("<class>"): return by_class.get(c[len("<class>"):], set())
    if c.startswith("<"): return ALL            # between tests: credit everyone
    return {num[c]} if c in num else ALL        # unknown context: everyone

reads, alls, derived, flags, groups = {}, {}, {}, {}, []
files = glob.glob(os.path.join(reads_dir, "valprof-*.json"))
assert files
for f in files:
    d = json.load(open(f))
    for s, kv in d["dict_reads"].items():
        for k, cs in kv.items():
            r = reads.setdefault(s, {}).setdefault(k, set())
            for c in cs: r |= ctx_tests(c)
    for s, cs in d["dict_all"].items():
        a = alls.setdefault(s, set())
        for c in cs: a |= ctx_tests(c)
    for s, ks in d["dict_derived"].items(): derived.setdefault(s, set()).update(ks)
    for s, fl in d["dict_flags"].items(): flags.setdefault(s, set()).update(fl)
    groups += d["dict_groups"]

# Merge groups that share an entry (union-find over (site, key)).
parent = {}
def find(x):
    while parent.setdefault(x, x) != x: x = parent[x]
    return x
for g in groups:
    items = [tuple(e) for e in g]
    for e in items[1:]: parent[find(e)] = find(items[0])
comp = {}
for e in list(parent): comp.setdefault(find(e), set()).add(e)
group_of = {}
for i, members in enumerate(comp.values()):
    for e in members: group_of[e] = i
glist = [sorted(map(list, m)) for m in comp.values()]

# Import-time {**X.NAME} inside a dict display that is not itself a class-body assignment value.
hazard = set()
for rel, info in M["files"].items():
    if not rel.startswith("sqlglot/"): continue
    tree = ast.parse(info["text"])
    def scan(node, ok_dict):
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): continue
            if isinstance(n, ast.Dict) and n is not ok_dict:
                for k, v in zip(n.keys, n.values):
                    if k is None:   # ** unpacking of a named dict: its entries are copied unrecorded
                        if isinstance(v, ast.Attribute): hazard.add(v.attr)
                        elif isinstance(v, ast.Name): hazard.add(v.id)
    def visit(body, in_class):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)): continue   # runs later: recorded
            if isinstance(st, ast.ClassDef):
                for d in st.decorator_list + st.bases: scan(d, None)
                visit(st.body, True); continue
            ok = st.value if (in_class and isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(st, "value", None), ast.Dict)) else None
            for fld, val in ast.iter_fields(st):
                if isinstance(val, list) and val and isinstance(val[0], ast.stmt): visit(val, in_class)
                elif isinstance(val, list):
                    for v in val:
                        if isinstance(v, ast.AST): scan(v, ok)
                elif isinstance(val, ast.AST): scan(val, ok)
    visit(tree.body, False)
    # functions that run while importing (metaclasses, __init_subclass__, builders): their merges
    # are exempted by the profiler too, so a ** of a named dict there is a hazard as well
    imp = set(info.get("import_lines", []))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Dict) and any(ln in imp for ln in range(n.lineno, n.end_lineno + 1)):
                for k, v in zip(n.keys, n.values):
                    if k is None:
                        if isinstance(v, ast.Attribute): hazard.add(v.attr)
                        elif isinstance(v, ast.Name): hazard.add(v.id)
                        # other expressions build a new dict through reads the profiler records
    # nested function bodies are skipped by scan's walk only at their own node; ast.walk still enters
    # them, which is conservative (more hazard names).

# Sites the profiler instrumented (valprof._Rewrite): a site there but absent below was never read.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import valprof
inst_sites = []
for rel, info in M["files"].items():
    if not rel.startswith("sqlglot/"): continue
    rw = valprof._Rewrite(rel); rw.visit(ast.parse(info["text"]))
    inst_sites += [f"{rel}:{ln}" for ln in rw.dict_sites]
M["dict_sites"] = sorted(inst_sites)

D = {}
for s in set(reads) | set(alls) | set(derived) | set(flags):
    D[s] = {"reads": {k: sid(v) for k, v in reads.get(s, {}).items()},
            "all": sid(alls[s]) if s in alls else -1,
            "derived": sorted(derived.get(s, ())), "flags": sorted(flags.get(s, ()))}
M["dicts"] = D
M["dict_groups"] = glist
M["dict_group_of"] = {f"{s}|{k}": i for (s, k), i in group_of.items()}
M["dict_merge_hazard"] = sorted(hazard)
M["dicts_profiled"] = True
json.dump(M, open(out, "w"), separators=(",", ":"))
print(f"dict sites {len(D)}, groups {len(glist)}, merge-hazard names {len(hazard)}", file=sys.stderr)
