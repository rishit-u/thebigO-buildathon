"""TPC-DS fingerprint lab (testing only): random code edits in functions the fingerprint check accepts
and that test_execute_tpcds runs. Truth: running test_execute_tpcds itself on the patched tree.
A miss = fingerprints unchanged (test dropped) but the test fails."""
import json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
TP = 1106
files = [rel for rel, info in F.m["files"].items() if rel.startswith("sqlglot/") and
         any(TP in F.m["sets"][k] for k in info["lines"].values())]
rows = []
tries = 0
while len(rows) < N and tries < 2000:
    tries += 1
    rel = rng.choice(files); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    new = "\n".join(lines)
    open(path, "w").write(new)
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, why = F.sel.select(patch)
    if TP not in picked or not SEL.tpcds_checkable(F.sel, SEL.parse_patch(patch)):
        open(path, "w").write(old); continue
    try: dropped = SEL.tpcds_unaffected(F.sel, patch)
    except Exception as e: dropped = False
    fails = F.run([F.ids[TP]])
    open(path, "w").write(old)
    failed = fails is None or TP in fails
    row = {"rel": rel, "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "dropped": dropped, "failed": failed, "miss": dropped and failed}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
