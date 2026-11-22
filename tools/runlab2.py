import json, os, random, re, subprocess, sys, importlib.util, resource
sys.path.insert(0, "/home/claude/work/tools"); sys.path.insert(0, "/tmp/appsim")
os.environ["BR_DATA"] = "/home/claude/work/syn"
import selector as S
S.log = lambda *a: None
import codelab_mut as M
sel = S.Selector(); SG = "/home/claude/work/sg"
T = json.load(open("/tmp/appsim/tests.json"))["tests"]; NUM = {t["id"]: t["number"] for t in T}; ID = {v: k for k, v in NUM.items()}
ms = {t["number"]: round(t["seconds"] * 1000) for t in T}; S.MS_ALL.update(ms)
rng = random.Random(int(sys.argv[1])); N = int(sys.argv[2])
files = [f for f in sel.files if f.startswith("sqlglot/") and len(sel.files[f]["text"]) > 1500]
n = 0; bad = 0; tot_gain = 0; tot_cpu = 0
while n < N:
    rel = rng.choice(files); p = os.path.join(SG, rel); old = open(p).read()
    new = M.mutate(old, rng.choice(["str", "int", "cmp", "not", "kwarg_bool", "del_stmt", "return_none", "method", "arith"]), rng)
    if not new: continue
    open(p, "w").write(new); patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout
    picked, why = sel.select(patch)
    picked = S.aggressive(sel, set(picked), [rel], NUM, patch) | (set(picked) & sel.protect)
    if not picked: open(p, "w").write(old); continue
    w = []; S._RUN_CPU[0] = 0
    out = S.run_or_select(sel, patch, set(picked), ms, ID, w)
    if not w or not w[-1].startswith("runner: ran"): open(p, "w").write(old); continue
    ids = sorted(ID[x] for x in picked)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE"] + ids, cwd=SG, capture_output=True, text=True, timeout=600)
    open(p, "w").write(old)
    truth = set()
    for l in re.findall(r"^(?:FAILED|ERROR|SUBFAILED).*?tests/\S+::\S+", r.stdout, re.M | re.S):
        m = re.search(r"(tests/\S+::\S+)", l) if l.startswith(("FAILED", "ERROR", "SUBFAILED")) else None
        if m: truth.add(NUM.get(re.sub(r"\[.*$", "", m.group(1)), -1))
    ok = truth <= set(out); bad += not ok; n += 1
    if not ok:
        open(f"/tmp/runbad_{n}.patch", "w").write(patch)
        print("BADIDS", sorted(ID.get(x, str(x)) for x in truth - set(out))[:5], "runner:", sorted(ID[x] for x in out)[:3], flush=True)
    gain = S.commit_cost(picked, ms) - S.commit_cost(set(out), ms); tot_gain += gain; tot_cpu += sys.modules["runner"].LAST_CPU
    print(json.dumps({"rel": rel, "picked": len(picked), "runner_failed": len(out), "pytest_failed": len(truth), "ok": ok, "gain": round(gain, 2), "cpu": round(sys.modules["runner"].LAST_CPU, 2), "extra": len(set(out) - truth)}), flush=True)
print("BAD", bad, "of", n, "gain", round(tot_gain, 1), "cpu", round(tot_cpu, 1))
