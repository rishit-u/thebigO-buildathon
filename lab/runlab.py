"""Runner lab (testing only): random code edits; when fast mode would hand the selection to the
in-container runner, compare the runner's output with the full-suite truth. Miss = failing test not output."""
import json, os, random, subprocess, sys
sys.path.insert(0, "/home/claude/lab")
import flowlab as F, mutlab as M
import selector as SEL
rng = random.Random(int(sys.argv[2])); N = int(sys.argv[1]); out = sys.argv[3]
tests = F.T; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
id_of = {t["number"]: t["id"] for t in tests}; num_of = {v: k for k, v in id_of.items()}
files = [f for f in F.m["files"] if f.startswith("sqlglot/")]
rows = []; tries = 0
while len(rows) < N and tries < 4000:
    tries += 1
    rel = rng.choice(files); path = os.path.join(F.TREE, rel); old = F.m["files"][rel]["text"]
    open(path, "w").write(old)
    k, lines = M.mutate_py(rng, path)
    if k != "code": continue
    open(path, "w").write("\n".join(lines))
    patch = subprocess.run(["git", "diff"], cwd=F.TREE, capture_output=True, text=True).stdout
    picked, why = F.sel.select(patch)
    paths = [f["new"] or f["old"] for f in SEL.parse_patch(patch)]
    fast = SEL.aggressive(F.sel, set(picked), paths, num_of, patch)
    fast = {n for n in fast if n in ms}
    w = []
    after = SEL.run_or_select(F.sel, patch, set(fast), ms, id_of, w) if fast else fast
    if not any("runner: ran" in x for x in w):
        open(path, "w").write(old); continue
    fails, tail = M.failing()
    open(path, "w").write(old)
    fails = set(fails)
    row = {"rel": rel, "diff": [l for l in patch.split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---")][:2],
           "n_sel": len(fast), "n_out": len(after), "fails": len(fails), "miss": sorted(fails - after),
           "fails_outside_sel": sorted(fails - fast)}
    rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(out, "w"))
