import json, os, random, re, subprocess, sys
sys.path.insert(0, "/tmp/appsim"); os.environ["BR_DATA"] = "/home/claude/work/syn"
import selector as S
S.log = lambda *a: None
sel = S.Selector(); SG = "/home/claude/work/sg"
T = json.load(open("/tmp/appsim/tests.json"))["tests"]; NUM = {t["id"]: t["number"] for t in T}; ID = {v: k for k, v in NUM.items()}
ms = {t["number"]: round(t["seconds"] * 1000) for t in T}
rng = random.Random(int(sys.argv[1])); N = int(sys.argv[2])
files = sys.argv[3].split(",")
def corrupt(line):
    words = line.split(" ")
    k = rng.randrange(len(words)); op = rng.choice(["ins", "del", "dup"])
    if op == "ins": words.insert(k, rng.choice(["42", "x", "0", "NULL", "1"]))
    elif op == "del" and len(words) > 1: del words[k]
    else: words.insert(k, words[k])
    return " ".join(words)
for _ in range(N):
    rel = rng.choice(files); p = os.path.join(SG, rel); old = open(p).read(); L = old.split("\n")
    while True:
        i = rng.randrange(len(L))
        if L[i].strip() and not L[i].startswith("--"): break
    L2 = list(L); L2[i] = corrupt(L[i])
    if L2[i] == L[i]: continue
    open(p, "w").write("\n".join(L2))
    patch = subprocess.run(["git", "diff", "--", rel], cwd=SG, capture_output=True, text=True).stdout
    picked, why = sel.select(patch)
    picked = S.aggressive(sel, set(picked), [rel], NUM, patch) | (set(picked) & sel.protect)
    readers = set(sel.sets[sel.reads[rel]])
    ids = sorted(ID[n] for n in readers)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "-n", "2"] + ids, cwd=SG, capture_output=True, text=True, timeout=900)
    open(p, "w").write(old)
    truth = set()
    for l in r.stdout.split("\n"):
        m = re.search(r"(tests/\S+::\S+)", l) if l.startswith(("FAILED", "ERROR", "SUBFAILED")) else None
        if m: truth.add(NUM.get(re.sub(r"\[.*$", "", m.group(1)), -1))
    miss = truth - picked
    print(json.dumps({"rel": rel[-10:], "line": i, "diff": L2[i].strip()[:60], "picked": len(picked), "cost": S.commit_cost(picked, ms),
                      "oracle": S.commit_cost({x for x in truth if x >= 0}, ms), "failed": sorted(ID.get(x, str(x)).split("::")[-1] for x in truth),
                      "MISS": sorted(ID.get(x, str(x)) for x in miss), "why": why[-1][-80:]}), flush=True)
