"""max_nodes lab (testing only): parser/tokenizer code edits on lines test_max_nodes runs, where the
fast selection is not broad (<=80% of tests). Truth: running test_max_nodes itself."""
import json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
MX = 1490
files = ["sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py", "sqlglot/errors.py"]
rows = []; tries = 0
while len(rows) < N and tries < 3000:
    tries += 1
    rel = rng.choice(files); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    open(path, "w").write("\n".join(lines))
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, why = F.sel.select(patch)
    if MX not in picked:
        open(path, "w").write(old); continue
    try: dropped = SEL.maxnodes_unaffected(F.sel, patch)
    except Exception: dropped = False
    fails = F.run([F.ids[MX]])
    open(path, "w").write(old)
    failed = fails is None or MX in fails
    row = {"rel": rel, "n_sel": len(picked), "failed": failed, "dropped": dropped, "miss": dropped and failed,
           "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2]}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
