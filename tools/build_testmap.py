"""testmap.json.gz: per-test line coverage of tests/**/*.py from the tcov plugin pass.
Usage: build_testmap.py <tcov dir> <tests.json> <sqlglot checkout> <out.json.gz>"""
import glob, gzip, json, os, sys
tdir, tj, src, out = sys.argv[1:5]
T = json.load(open(tj))["tests"]; num = {t["id"]: t["number"] for t in T}
by_class = {}
for t in T: by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
D = {}
for f in glob.glob(os.path.join(tdir, "tcov-*.json")):
    for ctx, v in json.load(open(f)).items():
        for p, ls in v.items():
            for ln in ls: D.setdefault(p, {}).setdefault(ln, set()).add(ctx)
sets, ids = [], {}
def sid(ns):
    k = tuple(sorted(ns))
    if k not in ids: ids[k] = len(sets); sets.append(list(k))
    return ids[k]
files = {}
for p, lines in D.items():
    L, imp, between = {}, [], []
    for ln, ctxs in lines.items():
        ns = set()
        for c in ctxs:
            if c == "<collect>": imp.append(ln)
            elif c == "<between>": between.append(ln)
            elif c.startswith("<class>"): ns |= by_class.get(c[7:], set()) or {-1}
            elif c in num: ns.add(num[c])
            else: between.append(ln)       # a context we cannot name (e.g. skipped test)
        if -1 in ns: between.append(ln); ns.discard(-1)
        if ns: L[str(ln)] = sid(ns)
    files[p] = {"lines": L, "import_lines": sorted(imp), "unknown_lines": sorted(set(between))}
json.dump({"sets": sets, "files": files}, gzip.open(out, "wt"), separators=(",", ":"))
print(len(files), "test files,", len(sets), "sets,", os.path.getsize(out), "bytes")
