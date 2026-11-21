"""Stratified full-suite lab (testing only), as requested in review: random code edits in strata that
fast mode treats specially, truth = the full suite. Reports misses of the v13 fast selection
(aggressive mode with TPC-DS fingerprints) and of safe mode."""
import json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
num_of = {t["id"]: t["number"] for t in F.T}
allf = [f for f in F.m["files"] if f.startswith("sqlglot/")]
STRATA = {
    "parser": [f for f in allf if f in ("sqlglot/parser.py",) or f.startswith("sqlglot/parsers/")],
    "tokenizer": ["sqlglot/tokens.py", "sqlglot/tokenizer_core.py"],
    "expressions": [f for f in allf if f.startswith("sqlglot/expressions/")],
    "generator": [f for f in allf if f == "sqlglot/generator.py" or f.startswith("sqlglot/generators/")],
    "dialects": [f for f in allf if f.startswith("sqlglot/dialects/")],
    "optimizer": [f for f in allf if f.startswith("sqlglot/optimizer/")],
}
names = list(STRATA)
rows = []
while len(rows) < N:
    stratum = names[len(rows) % len(names)]
    rel = rng.choice(STRATA[stratum]); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    open(path, "w").write("\n".join(lines))
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, why = F.sel.select(patch)
    paths = [f["new"] or f["old"] for f in SEL.parse_patch(patch)]
    try: fast = SEL.aggressive(F.sel, set(picked), paths, num_of, patch)
    except Exception as e: fast = set(picked)
    fails, tail = M.failing()
    open(path, "w").write(old)
    fails = set(fails)
    row = {"stratum": stratum, "rel": rel,
           "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "fails": len(fails), "miss_safe": sorted(fails - set(picked)), "miss_fast": sorted(fails - fast),
           "n_safe": len(picked), "n_fast": len(fast)}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
