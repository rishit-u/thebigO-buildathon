"""Blast Radius selector, v1: coverage map + fixture map, conservative fallbacks.

For each patch, decide which tests could possibly fail, then split them over
the cheapest number of workers. When unsure, run more: a miss costs 300 s.
"""
import ast, bisect, gzip, json, re, sys
from pathlib import Path

import os
DATA = Path(os.environ.get("BR_DATA", "/data"))
MAP = Path(__file__).with_name("map.json.gz")
WORKER_MS = 2000

def log(*a): print(*a, file=sys.stderr)

# ---------- reading a unified diff ----------
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

def parse_patch(text):
    """-> list of {old, new, hunks:[(old_start, [(op, line)])]}; op is ' ', '-', '+'."""
    files, cur, hunk = [], None, None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": []}; files.append(cur); hunk = None
        elif cur is None:
            continue
        elif hunk is None and line.startswith("--- "):
            cur["old"] = None if line[4:].strip() == "/dev/null" else line[4:].strip()[2:]
        elif hunk is None and line.startswith("+++ "):
            cur["new"] = None if line[4:].strip() == "/dev/null" else line[4:].strip()[2:]
        elif line.startswith("@@"):
            m = HUNK.match(line)
            hunk = (int(m.group(1)), []); cur["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append((line[0], line[1:]))
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        elif hunk is not None and line == "":
            pass
        else:
            hunk = None  # a git header line (rename, mode, index) between files
    for f in files:
        if f["old"] is None and f["new"] is None:  # rename/mode-only: take names from header
            pass
    return files

def apply(old_text, hunks):
    old = old_text.split("\n"); out, i = [], 0
    for start, body in hunks:
        s = start - 1 if start > 0 else 0
        out.extend(old[i:s]); i = s
        for op, l in body:
            if op == " ": out.append(old[i]); i += 1
            elif op == "-": i += 1
            else: out.append(l)
    out.extend(old[i:])
    return "\n".join(out)

CONTINUATION = {"elif", "else", "except", "finally", "case"}
BLOCK = {"if", "for", "while", "try", "with", "match", "async", "def", "class", "lambda"}

def first_word(l):
    m = re.match(r"\s*([A-Za-z_]+)", l)
    return m.group(1) if m else ""

def control_flow_change(hunks):
    """Does this edit change which code runs, rather than what one statement does?"""
    for _, body in hunks:
        removed = [l for op, l in body if op == "-"]; added = [l for op, l in body if op == "+"]
        words_r = [first_word(l) for l in removed]; words_a = [first_word(l) for l in added]
        if any(w in CONTINUATION for w in words_r + words_a): return True
        if sorted(w for w in words_r if w in BLOCK) != sorted(w for w in words_a if w in BLOCK): return True
        indent = lambda l: len(l) - len(l.lstrip())
        if sorted(indent(l) for l in removed if l.strip()) != sorted(indent(l) for l in added if l.strip()):
            if added or removed: return True
    return False

def changed_old_lines(hunks):
    """Old-file lines removed or edited, and insertion points (old line before which new lines go)."""
    removed, inserted_before = set(), set()
    for start, body in hunks:
        ln = start
        for op, _ in body:
            if op == " ": ln += 1
            elif op == "-": removed.add(ln); ln += 1
            else: inserted_before.add(ln)
    return removed, inserted_before

# ---------- deciding tests ----------
GLOBAL_CLASS_ATTRS = {"_sql_names"}   # read at import into registries shared by every dialect

def registry_change(path, old, new):
    """Edits whose effect is not limited to users of this module's classes. '' if none."""
    if path.endswith("__init__.py"): return "package __init__"
    def imports(t): return [ast.dump(n) for n in ast.walk(t) if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports(old) != imports(new): return "imports changed"
    def classes(t):
        out = {}
        def visit(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = prefix + n.name
                    attrs = {}
                    for st in n.body:
                        targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                        for tg in targets:
                            if isinstance(tg, ast.Name) and tg.id in GLOBAL_CLASS_ATTRS:
                                attrs[tg.id] = ast.dump(st.value) if st.value is not None else ""
                    out[q] = (ast.dump(ast.Tuple(n.bases, ast.Load())), str([ast.dump(k) for k in n.keywords]), attrs)
                    visit(n.body, q + ".")
        visit(t.body, "")
        return out
    co, cn = classes(old), classes(new)
    if set(co) != set(cn): return "classes added/removed/renamed"
    for q in co:
        if co[q][:2] != cn[q][:2]: return f"bases of {q} changed"
        if co[q][2] != cn[q][2]: return f"registry attribute of {q} changed"
    return ""
class Selector:
    def __init__(self):
        m = json.loads(gzip.decompress(MAP.read_bytes()))
        self.sets = m["sets"]; self.files = m["files"]; self.reads = m["reads"]
        self.test_files = m["test_files"]; self.ALL = set(self.sets[m["all"]])

    def py_source(self, path, hunks, why):
        info = self.files[path]
        new_text = apply(info["text"], hunks)
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            why.append(f"{path}: syntax error -> all"); return self.ALL
        readers = set(self.sets[self.reads[path]]) if path in self.reads else set()  # tests that read this source as text
        if ast.dump(new_tree) == info["ast"]:
            why.append(f"{path}: same AST (comment/whitespace) -> {len(readers)} source readers"); return readers
        mod_users = set(self.sets[info["mod_users"]])
        registry = registry_change(path, ast.parse(info["text"]), new_tree)
        spans = info["spans"]; starts = [s[0] for s in spans]
        lines, users = info["lines"], set(self.sets[info["users"]])
        import_lines = set(info.get("import_lines", []))
        removed, inserted = changed_old_lines(hunks)
        picked = set()
        reroutes = control_flow_change(hunks)

        def entered(ln):
            """Tests that entered the innermost function around line ln."""
            best = None
            for a, b in info["funcs"]:
                if a - 1 <= ln <= b + 1 and (best is None or b - a < best[1] - best[0]): best = (a, b)
            if best is None: return None
            ts = set()
            for x in range(best[0], best[1] + 1):
                if str(x) in lines: ts |= set(self.sets[lines[str(x)]])
            return ts

        def stmt_at(ln):
            k = bisect.bisect_right(starts, ln) - 1
            while k >= 0:
                s, e, scope = spans[k]
                if s <= ln <= e: return spans[k]
                if e < ln and spans[k][2] == "m": return None
                k -= 1
            return None

        def tests_for_stmt(st):
            s, e, scope = st
            if scope == "m":   # runs at import: who depends on it is who uses this module's objects
                if registry: return self.ALL, f"module-level, {registry}"
                return mod_users, "module-level"
            if any(ln in import_lines for ln in range(s, e + 1)):
                return self.ALL, "runs at import"   # e.g. metaclasses, factories called in class bodies
            ts = set()
            for ln in range(s, e + 1):
                if str(ln) in lines: ts |= set(self.sets[lines[str(ln)]])
            return ts, "function"

        for ln in sorted(removed):
            st = stmt_at(ln)
            if st is None:   # blank/comment line outside any statement: look at neighbours
                inserted.add(ln); continue
            ts, kind = tests_for_stmt(st); picked |= ts
            if kind != "function": why.append(f"{path}:{ln} {kind} -> {len(ts)}")
        for ln in sorted(inserted):
            for nb in (ln - 1, ln):   # statements just above and at the insertion point
                st = stmt_at(nb)
                if st is None: continue
                ts, kind = tests_for_stmt(st); picked |= ts
            if stmt_at(ln - 1) is None and stmt_at(ln) is None:
                if registry: picked |= self.ALL; why.append(f"{path}:{ln} insert, {registry} -> all")
                else: picked |= mod_users; why.append(f"{path}:{ln} insert at module level -> {len(mod_users)}")
        if reroutes:   # control flow changed: anyone who entered the function may take a new path
            for ln in sorted(removed | inserted):
                st = stmt_at(ln) or stmt_at(ln - 1)
                if st is not None and st[2] == "m": continue   # module level: handled above
                ts = entered(ln)
                if ts is None: picked |= mod_users; why.append(f"{path}:{ln} control flow outside a function -> {len(mod_users)}")
                else: picked |= ts; why.append(f"{path}:{ln} control flow -> {len(ts)} who entered the function")
        why.append(f"{path}: {len(picked)} tests")
        return picked | readers

    def select(self, patch_text):
        why, picked = [], set()
        for f in parse_patch(patch_text):
            path = f["new"] or f["old"]
            if f["old"] in self.files and f["new"] == f["old"] and path.endswith(".py"):
                picked |= self.py_source(path, f["hunks"], why)
            elif f["old"] == f["new"] and path in self.reads:
                picked |= set(self.sets[self.reads[path]]); why.append(f"{path}: fixture readers")
            elif f["old"] == f["new"] and path in self.test_files:
                picked |= set(self.sets[self.test_files[path]]); why.append(f"{path}: test file")
            else:
                why.append(f"{path}: unknown kind of change -> all"); return self.ALL, why
        return picked, why

# ---------- splitting over workers ----------
def split(nums, ms):
    """Try 1..8 workers, longest test first onto the lightest worker; keep the cheapest."""
    if not nums: return []
    order = sorted(nums, key=lambda n: -ms[n]); best = None
    for w in range(1, min(8, len(order)) + 1):
        workers = [[] for _ in range(w)]; loads = [0] * w
        for n in order:
            i = loads.index(min(loads)); workers[i].append(n); loads[i] += ms[n]
        cost = max(loads) + WORKER_MS * w
        if best is None or cost < best[0]: best = (cost, workers)
    return best[1]

def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sel = Selector()
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            nums, why = sel.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never crash: a crash rejects the run, a wide answer only costs time
            nums, why = sel.ALL, [f"error {e!r} -> all"]
        nums = {n for n in nums if n in ms}
        print(json.dumps({"commit": patch.stem, "workers": split(nums, ms)}))
        log(patch.stem, len(nums), "tests |", "; ".join(why))

if __name__ == "__main__":
    main()
