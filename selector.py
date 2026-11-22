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
    """Old-file lines removed or edited, and insertion points (old line before which new lines go).
    A run of k removed lines followed by j added lines is an edit of those k lines; only when j > k
    do the extra added lines count as an insertion, right after the last removed line."""
    removed, inserted_before = set(), set()
    for start, body in hunks:
        ln = start; i = 0
        while i < len(body):
            op = body[i][0]
            if op == " ":
                ln += 1; i += 1; continue
            run_minus = run_plus = 0
            while i < len(body) and body[i][0] == "-": removed.add(ln); ln += 1; run_minus += 1; i += 1
            while i < len(body) and body[i][0] == "+": run_plus += 1; i += 1
            if run_plus > run_minus: inserted_before.add(ln)
    return removed, inserted_before

# ---------- deciding tests ----------
def scalar_literal(v):
    if isinstance(v, ast.Constant): return True
    if isinstance(v, ast.UnaryOp) and isinstance(v.operand, ast.Constant): return True
    if isinstance(v, ast.Tuple): return all(scalar_literal(e) for e in v.elts)
    return False

def new_scalar_ok(tree, cls, name):
    """In the patched file, is cls.name still assigned a scalar literal (or no longer assigned)?"""
    body = tree.body
    for part in cls.split("."):
        nxt = [n for n in body if isinstance(n, ast.ClassDef) and n.name == part]
        if len(nxt) != 1: return False
        body = nxt[0].body
    found = []
    for st in body:
        tg = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in tg): found.append(st)
    if not found: return True
    return len(found) == 1 and isinstance(found[0], (ast.Assign, ast.AnnAssign)) and found[0].value is not None and scalar_literal(found[0].value)

SIBLINGS = {frozenset({ast.Is, ast.Eq}): "eq", frozenset({ast.IsNot, ast.NotEq}): "eq",
            frozenset({ast.Lt, ast.LtE}): "ord", frozenset({ast.Gt, ast.GtE}): "ord"}

def bool_struct(node, leaves):
    if isinstance(node, ast.BoolOp):
        return ("and" if isinstance(node.op, ast.And) else "or", [bool_struct(v, leaves) for v in node.values])
    leaves.append(ast.dump(node)); return len(leaves) - 1

def leaf_nodes(node, out):
    if isinstance(node, ast.BoolOp):
        for v in node.values: leaf_nodes(v, out)
    else: out.append(node)

def eval3(struct, truths, used):
    """Python's short-circuit evaluation over recorded leaf truths; None if an unrecorded leaf is needed."""
    if isinstance(struct, int):
        used.add(struct); return truths[struct]
    op, kids = struct
    for k in kids:
        v = eval3(k, truths, used)
        if v is None: return None
        if (op == "and" and not v) or (op == "or" and v): return v
    return op == "and"

def operator_swaps(old, new):
    """If the two trees differ only in single-operator comparisons (siblings or not) and in the and/or
    structure of conditions with identical operands, list the changes: (line, col, kind, detail).
    kind is a comparison family ("eq"/"ord"), None for a non-sibling comparison swap, or "bool" with
    (old structure, new structure). Otherwise return None."""
    out = []
    def same(a, b):
        if type(a) is not type(b): return False
        if isinstance(a, ast.BoolOp):
            la, lb = [], []
            sa, sb = bool_struct(a, la), bool_struct(b, lb)
            if sa == sb:   # same and/or structure: operands may differ by comparison swaps
                na, nb = [], []
                leaf_nodes(a, na); leaf_nodes(b, nb)
                return len(na) == len(nb) and all(same(x, y) for x, y in zip(na, nb))
            if la != lb: return False
            out.append((a.lineno, a.col_offset, "bool", (sa, sb)))
            return True
        if isinstance(a, ast.AST):
            for field in a._fields:
                va, vb = getattr(a, field, None), getattr(b, field, None)
                if isinstance(a, ast.Compare) and field == "ops":
                    ta, tb = [type(x) for x in va], [type(x) for x in vb]
                    if ta != tb:
                        if len(ta) != 1 or len(tb) != 1: return False
                        out.append((a.lineno, a.col_offset, SIBLINGS.get(frozenset({ta[0], tb[0]})), None))
                    continue
                if not same(va, vb): return False
            return True
        if isinstance(a, list): return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b
    return out if same(old, new) else None

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
        self.profiled = m.get("profiled", False)
        self.tpcds = m.get("tpcds")
        self.m = {k: m.get(k) for k in ("dicts", "dict_groups", "dict_group_of", "dict_sites", "dict_merge_hazard", "dicts_profiled")}

    def dict_value_edit(self, path, old_tree, new_tree, why):
        """Fast mode: the only change is to values (not keys) of dicts assigned in class bodies.
        Selects the tests that read those entries (d[k] / d.get(k)) or all values of the dict, in
        that dict or in dicts holding the very same value objects (class-body {**Parent.X} merges).
        None when the rule does not apply (then the normal rules run)."""
        import copy
        def entries(tree):
            out = {}
            def walk(body, q):
                for st in body:
                    if isinstance(st, ast.ClassDef):
                        qq = (q + "." if q else "") + st.name
                        for x in st.body:
                            if isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name) and isinstance(x.value, ast.Dict):
                                out[(qq, x.targets[0].id)] = x
                        walk(st.body, qq)
            walk(tree.body, ""); return out
        def stripped(tree):
            t = copy.deepcopy(tree)
            for x in entries(t).values(): x.value.values = [ast.Constant(None) for _ in x.value.values]
            return ast.dump(t)
        old_e, new_e = entries(old_tree), entries(new_tree)
        if not old_e or stripped(old_tree) != stripped(new_tree): return None
        D, groups, gof = self.m["dicts"], self.m["dict_groups"], self.m["dict_group_of"]
        inst, hazard = set(self.m["dict_sites"]), set(self.m["dict_merge_hazard"])
        def key_reprs(k):
            if isinstance(k, ast.Constant) and isinstance(k.value, (str, int, float, bool, type(None))): return [repr(k.value)]
            if isinstance(k, ast.Attribute):
                base = k.value.attr if isinstance(k.value, ast.Attribute) else getattr(k.value, "id", None)
                return [k.attr] + ([f"{base}.{k.attr}"] if base else [])
            if isinstance(k, ast.Name): return [k.id]
            return None
        def objects_of(site):
            """Tests that created an object of the class holding this site (or a subclass), plus
            tests whose own code names the class; None if unknown."""
            rel, ln = site.rsplit(":", 1); ln = int(ln); best = None
            for e in self.files.get(rel, {}).get("classes", []):
                if e[1] <= ln <= e[2] and (best is None or e[1] >= best[1]): best = e
            if best is None or len(best) < 5 or best[4] < 0: return None
            return set(self.sets[best[4]])
        def site_tests(site, keys):
            d = D.get(site, {"reads": {}, "all": -1, "derived": [], "flags": []})
            if d["flags"]: return None
            extra = set()
            if "*" in d["derived"] or any(k in d["derived"] for k in keys):
                # Expression.arg_types flags are read at import only by __init_subclass__ to derive
                # the class's required_args, which is used when an object of the class is validated
                if not (site_tests.flag_flip and site_tests.name == "arg_types"): return None
                extra = objects_of(site)
                if extra is None: return None
            ts = set(self.sets[d["all"]]) if d["all"] >= 0 else set()
            ts |= extra
            for k in keys:
                if k in d["reads"]: ts |= set(self.sets[d["reads"][k]])
            return ts
        picked, n_entries = set(), 0
        for key, o in old_e.items():
            n = new_e[key]
            changed = [i for i, (a, b) in enumerate(zip(o.value.values, n.value.values)) if ast.dump(a) != ast.dump(b)]
            if not changed: continue
            site = f"{path}:{o.lineno}"
            if site not in inst or key[1] in hazard: return None
            site_tests.name = key[1]
            site_tests.flag_flip = all(isinstance(v.value.values[i], ast.Constant) and type(v.value.values[i].value) is bool
                                       for v in (o, n) for i in changed)
            for i in changed:
                ks = key_reprs(o.value.keys[i])
                if ks is None: return None
                todo = {(site, k) for k in ks}
                for k in ks:   # entries holding the same value object (merged copies)
                    g = gof.get(f"{site}|{k}")
                    if g is not None: todo |= {tuple(e) for e in groups[g]}
                for s2, k2 in todo:
                    if s2 != site and s2 not in inst: return None
                    ts = site_tests(s2, [k2] if s2 != site else ks)
                    if ts is None: return None
                    picked |= ts
                n_entries += 1
        if not n_entries: return None
        why.append(f"{path}: {n_entries} class-body dict value(s) changed -> {len(picked)} tests that read them")
        return picked

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
        old_tree = ast.parse(info["text"])
        if self.profiled:
            swaps = operator_swaps(old_tree, new_tree)
            ok = swaps and all(fam in ("eq", "ord") or (fam == "bool" and f"{ln}:{col}" in info.get("bool_sites", {}))
                               for ln, col, fam, _ in swaps)
            if ok:
                picked = set(readers)
                for ln, col, fam, detail in swaps:
                    if fam == "bool":
                        old_s, new_s = detail; ts = set()
                        for truths, sid_ in info["bool_sites"][f"{ln}:{col}"]:
                            if "?" in truths:
                                if sid_ == -1: why.append(f"{path}:{ln} and/or change, custom truthiness at import -> all"); return self.ALL
                                ts |= set(self.sets[sid_]); continue
                            u_old, u_new = set(), set()
                            r_old = eval3(old_s, truths, u_old); r_new = eval3(new_s, truths, u_new)
                            if r_new is None or bool(r_new) != bool(r_old) or u_new != u_old:
                                if sid_ == -1: why.append(f"{path}:{ln} and/or change, hazard at import -> all"); return self.ALL
                                ts |= set(self.sets[sid_])
                        picked |= ts; why.append(f"{path}:{ln} and/or change -> {len(ts)} tests where it could differ")
                        continue
                    sid_ = info.get("cmp_hazard", {}).get(f"{ln}:{col}", {}).get(fam)
                    if sid_ == -1: why.append(f"{path}:{ln} operator swap, hazard at import -> all"); return self.ALL
                    ts = set(self.sets[sid_]) if sid_ is not None else set()
                    picked |= ts; why.append(f"{path}:{ln} {fam} operator swap -> {len(ts)} tests where it could differ")
                return picked
        if AGGRESSIVE and self.m.get("dicts_profiled"):
            r = self.dict_value_edit(path, old_tree, new_tree, why)
            if r is not None: return readers | r
        registry = registry_change(path, old_tree, new_tree)
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

        def class_reach(s):
            """Aggressive mode: a statement in a class body (not its header) reaches the tests that
            created an object with that class, or an enclosing class, in its MRO."""
            inner = None
            for q, a, b, sid_, *_ in info.get("classes", []):
                if a <= s <= b and (inner is None or a >= inner[1]): inner = (q, a, b, sid_)
            if inner is None or s == inner[1]: return None
            ts = set()
            for q, a, b, sid_, *_ in info.get("classes", []):
                if a <= s <= b:
                    if sid_ < 0: return None
                    ts |= set(self.sets[sid_])
            return ts, f"class body of {inner[0]}"

        def in_lambda(ln):
            """Tests that called the innermost lambda whose body holds ln, if ln is runtime-only code."""
            if ln in import_lines: return None
            best = None
            for a, b in info.get("lambdas", []):
                if a <= ln <= b and (best is None or b - a < best[1] - best[0]): best = (a, b)
            if best is None: return None
            ts = set()
            for x in range(best[0], best[1] + 1):
                if str(x) in lines: ts |= set(self.sets[lines[str(x)]])
            return ts or None   # no line of the lambda ever ran: coverage may be incomplete, use the file level

        def scalar_attr(ln):
            """Readers of a scalar class attribute whose assignment holds ln, if the patch keeps it a
            scalar literal (or removes it). None if ln is not such an assignment."""
            for a, b, cls, name, set_id in info.get("scalar_attrs", []):
                if a <= ln <= b:
                    if new_scalar_ok(new_tree, cls, name): return set(self.sets[set_id]), f"{cls}.{name}"
                    return None
            return None

        def tests_for_stmt(st, ln=None):
            s, e, scope = st
            if scope == "m" and ln is not None and not registry:
                lam = in_lambda(ln)
                if lam is not None: return lam, "lambda body"
                sa = scalar_attr(ln)
                if sa is not None: return sa[0], f"reads of {sa[1]}"
            if scope == "m":   # runs at import: who depends on it is who uses this module's objects
                if registry: return self.ALL, f"module-level, {registry}"
                if AGGRESSIVE:
                    cr = class_reach(s)
                    if cr is not None: return cr
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
            ts, kind = tests_for_stmt(st, ln); picked |= ts
            if kind != "function": why.append(f"{path}:{ln} {kind} -> {len(ts)}")
        for ln in sorted(inserted):
            for nb in (ln - 1, ln):   # statements just above and at the insertion point
                st = stmt_at(nb)
                if st is None: continue
                ts, kind = tests_for_stmt(st, nb); picked |= ts
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

# ---------- aggressive mode (opt-in, accepts miss risk for speed) ----------
# Keep a giant test only when the edit is in code that test specifically checks; replace
# run-everything fallbacks with the tests that ran any line of the changed files.
AGGRESSIVE = os.environ.get("BR_SAFE") != "1"
GIANT_AREAS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("sqlglot/executor/", "sqlglot/planner.py", "sqlglot/optimizer/",
        "sqlglot/serde.py", "sqlglot/generators/python.py", "sqlglot/generators/duckdb.py", "sqlglot/dialects/duckdb.py",
        "sqlglot/parsers/duckdb.py", "tests/fixtures/optimizer/tpc-ds/"),
    "tests/test_parser.py::TestParser::test_max_nodes": ("sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py",
        "sqlglot/errors.py"),
    "tests/test_generator.py::TestGenerator::test_generator_memory": ("sqlglot/generator.py", "sqlglot/dialects/dialect.py"),
}

# ---------- TPC-DS stage fingerprints (fast mode) ----------
# test_execute_tpcds = parse -> pickle -> optimize -> Plan -> Python executor, compared with DuckDB on
# transpile(write="duckdb"). If the patch only changes functions that run inside the first stages,
# and the patched stages give the same structural hashes as the pinned ones for every query, the
# executor gets identical inputs and runs identical code: the test cannot change outcome.
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_CHECK = True    # v16: result-level TPC-DS check (tpcds_res.py)
APP = Path(__file__).resolve().parent
PINNED_DIR, PINNED_FP, SPEC = APP / "pinned", APP / "tpcds_pinned.json", APP / "tpcds_spec.json"
TPCDS_RESULTS, TPCDS_DATA = APP / "tpcds_results.json", APP / "tpcds_data"
TPCDS_PERQ = APP / "tpcds_perq.json"     # executor lines run by each TPC-DS query (pinned run)

def write_tree(sel, root, changed=None):
    for rel, info in sel.files.items():
        if not rel.startswith("sqlglot/"): continue
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((changed or {}).get(rel, info["text"]), encoding="utf-8")

def run_fp(root, timeout=40, pinned=None):
    import subprocess
    r = subprocess.run([sys.executable, "-I", str(APP / "tpcds_fp.py"), str(root), str(SPEC)] + ([str(pinned)] if pinned else []),
                       capture_output=True, text=True, timeout=timeout,
                       env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")})
    if r.returncode != 0: raise RuntimeError("fingerprint run failed: " + r.stderr[-300:])
    return json.loads(r.stdout)

MAXNODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"
MAXNODES_PINNED = APP / "maxnodes_pinned.json"

def run_script(script, root, timeout=8):   # normal run ~1 s; a hang must not burn CPU
    import subprocess
    r = subprocess.run([sys.executable, "-I", str(APP / script), str(root)], capture_output=True, text=True,
                       timeout=timeout, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")})
    if r.returncode != 0: raise RuntimeError(script + " failed: " + r.stderr[-300:])
    return json.loads(r.stdout)

def patched_tree(sel, files):
    import shutil, tempfile
    root = Path(tempfile.mkdtemp(prefix="brfp", dir="/tmp"))
    if PINNED_DIR.exists(): shutil.copytree(PINNED_DIR, root, dirs_exist_ok=True)
    else: write_tree(sel, root)
    for f in files:
        p = root / f["new"]
        p.write_text(apply(sel.files[f["new"]]["text"], f["hunks"]), encoding="utf-8")
        pyc = p.parent / "__pycache__"
        for c in (pyc.glob(p.stem + ".*.pyc") if pyc.exists() else []): c.unlink()
    return root

def body_only(sel, files):
    """Every change is inside the body of a function in a sqlglot .py file; no registry change."""
    for f in files:
        path = f["new"] or f["old"]
        if not (path and f["old"] == f["new"] and path.startswith("sqlglot/") and path.endswith(".py") and path in sel.files):
            return False
        old_text = sel.files[path]["text"]; old_tree = ast.parse(old_text)
        if registry_change(path, old_tree, ast.parse(apply(old_text, f["hunks"]))): return False
        removed, inserted = changed_old_lines(f["hunks"])
        fns = [n for n in ast.walk(old_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for ln in removed | inserted:
            if not any(fn.body[0].lineno <= ln <= fn.end_lineno for fn in fns): return False
    return True

def maxnodes_unaffected(sel, text):
    """test_max_nodes only parses: if the patched parser gives identical results (trees, limit errors,
    messages) on scaled-down versions of its inputs, its outcome cannot change."""
    if not MAXNODES_PINNED.exists(): return False
    files = parse_patch(text)
    # the check imports the patched sqlglot afresh, so module-level, class-body and registry changes
    # take effect in it too; only changes outside sqlglot's own .py files are out of its reach
    if not all(f["old"] == f["new"] and f["new"] and f["new"].startswith("sqlglot/") and f["new"].endswith(".py")
               and f["new"] in sel.files for f in files): return False
    import shutil
    root = patched_tree(sel, files)
    try:
        return run_script("maxnodes_fp.py", root) == json.loads(MAXNODES_PINNED.read_text())
    finally:
        shutil.rmtree(root, ignore_errors=True)

def build_pinned(sel):
    """At image build: pinned tree, spec, and pinned fingerprints (two runs must agree)."""
    import compileall
    SPEC.write_text(json.dumps({"queries": sel.tpcds["queries"], "schema": sel.tpcds["schema"]}))
    write_tree(sel, PINNED_DIR)
    compileall.compile_dir(str(PINNED_DIR), quiet=1)
    a, b = run_fp(PINNED_DIR, 300), run_fp(PINNED_DIR, 300)
    if a != b: log("pinned fingerprints are not deterministic: feature off"); return
    PINNED_FP.write_text(json.dumps(a)); log("pinned TPC-DS fingerprints:", len(a))
    import subprocess
    r = subprocess.run([sys.executable, "-I", str(APP / "tpcds_res.py"), str(PINNED_DIR), str(SPEC), str(PINNED_FP),
                        "-", str(TPCDS_DATA), "--build"], capture_output=True, text=True, timeout=600)
    if r.returncode == 0: TPCDS_RESULTS.write_text(r.stdout); log("pinned TPC-DS results:", len(json.loads(r.stdout)))
    else: log("pinned TPC-DS results failed: check off")
    if not RUNNABLE_PATH.exists() and (APP / "tests.json").exists() and (PINNED_DIR / "tests").exists():
        # runnable tests for the runner (normally shipped prebuilt: running the suite twice is slow)
        import runner
        runner.build(str(APP / "tests.json")); log("runner: runnable tests", len(json.loads(RUNNABLE_PATH.read_text())))
    a, b = run_script("maxnodes_fp.py", PINNED_DIR, 300), run_script("maxnodes_fp.py", PINNED_DIR, 300)
    if a != b: log("pinned max_nodes results are not deterministic: check off"); return
    MAXNODES_PINNED.write_text(json.dumps(a)); log("pinned max_nodes results:", len(a))

_PERQ = None
def tpcds_checkable(sel, files):
    """None: cannot be checked (keep TPC-DS). Otherwise the set of executed queries that must be re-run
    beyond those whose stage hashes change: empty when every change runs only before the executor;
    for changes inside executor-stage functions, a small set of queries that together run every
    changed executor line (coverage-based minimization, v18)."""
    global _PERQ
    other = sel.tpcds["other"]
    if _PERQ is None:
        _PERQ = json.loads(TPCDS_PERQ.read_text()) if TPCDS_PERQ.exists() else {}
    force_lines = []
    for f in files:
        path = f["new"] or f["old"]
        if not (path and f["old"] == f["new"] and path.startswith("sqlglot/") and path.endswith(".py") and path in sel.files):
            return None
        old_text = sel.files[path]["text"]; old_tree = ast.parse(old_text)
        new_tree = ast.parse(apply(old_text, f["hunks"]))
        if registry_change(path, old_tree, new_tree): return None
        removed, inserted = changed_old_lines(f["hunks"])
        tops = [n for n in ast.walk(old_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        bad = set(other.get(path, []))
        perq = _PERQ.get(path, {})
        for ln in removed | inserted:
            outer = None
            for fn in tops:   # outermost function whose body holds ln
                if fn.body[0].lineno <= ln <= fn.end_lineno and (outer is None or fn.lineno < outer.lineno):
                    outer = fn
            if outer is None:                                     # module or class level: the check imports
                if bad: return None                               # the patched package afresh, so this is
                continue                                          # covered unless the file has executor code
            span = set(range(outer.lineno, outer.end_lineno + 1)) & bad
            if not span: continue
            # executor-stage function: every one of its executor lines must be run per query (else keep)
            for l2 in span:
                if str(l2) not in perq: return None
                force_lines.append(set(perq[str(l2)]))
    if not force_lines: return set()
    chosen = set()
    for qs in force_lines:                                       # greedy cover: each line run at least once
        if not (qs & chosen):
            chosen.add(max(qs, key=lambda q: sum(1 for o in force_lines if q in o)))
    return chosen

def tpcds_unaffected(sel, text):
    if not (sel.tpcds and PINNED_FP.exists() and TPCDS_RESULTS.exists()): return False
    files = parse_patch(text)
    force = tpcds_checkable(sel, files)
    if force is None: return False
    import shutil, tempfile
    root = Path(tempfile.mkdtemp(prefix="brfp", dir="/tmp"))
    try:
        if PINNED_DIR.exists(): shutil.copytree(PINNED_DIR, root, dirs_exist_ok=True)
        for f in files:
            p = root / f["new"]
            p.write_text(apply(sel.files[f["new"]]["text"], f["hunks"]), encoding="utf-8")
            pyc = p.parent / "__pycache__"
            for c in (pyc.glob(p.stem + ".*.pyc") if pyc.exists() else []): c.unlink()
        import subprocess
        r = subprocess.run([sys.executable, "-I", str(APP / "tpcds_res.py"), str(root), str(SPEC), str(PINNED_FP),
                            str(TPCDS_RESULTS), str(TPCDS_DATA)] + (["--force", ",".join(map(str, sorted(force)))] if force else []),
                           capture_output=True, text=True, timeout=30,
                           env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")})
        res = json.loads(r.stdout) if r.returncode == 0 else {"ok": False, "why": r.stderr[-200:]}
        log("  TPC-DS result check:", res)
        return res.get("ok") is True
    finally:
        shutil.rmtree(root, ignore_errors=True)

def aggressive(sel, picked, paths, num_of, text=None):
    was_all = len(picked) >= len(sel.ALL) - 1
    if was_all:   # run-everything fallback: tests that ran the changed files
        narrowed = set()
        for p in paths:
            if p in sel.files: narrowed |= set(sel.sets[sel.files[p]["users"]])
            elif p in sel.reads: narrowed |= set(sel.sets[sel.reads[p]])
            elif p in sel.test_files: narrowed |= set(sel.sets[sel.test_files[p]])
            else: return picked            # unknown kind of file: keep everything
        picked = narrowed
    # a very broad edit (>80% of tests) keeps the giants too, except after a run-everything fallback
    # (registry changes such as a new class): there the giants follow their code areas (v14p probe)
    broad = not was_all and len(picked) * 5 > len(sel.ALL) * 4
    for tid, areas in GIANT_AREAS.items():
        n = num_of.get(tid)
        if n in picked and not broad and not any(p.startswith(a) for p in paths for a in areas):
            picked = picked - {n}
    n = num_of.get(MAXNODES_ID)
    if n in picked and text is not None:
        try:
            if maxnodes_unaffected(sel, text): picked = picked - {n}; log("  max_nodes inputs parse identically: dropped")
            else: log("  max_nodes kept (not checkable, or results changed)")
        except Exception as e:
            log("  max_nodes check failed, keeping it:", repr(e)[:200])
    n = num_of.get(TPCDS_ID)
    if n in picked and text is not None and TPCDS_CHECK:
        try:
            if tpcds_unaffected(sel, text): picked = picked - {n}; log("  TPC-DS stage fingerprints unchanged: dropped")
            else: log("  TPC-DS kept (not checkable, or fingerprints changed)")
        except Exception as e:
            log("  TPC-DS fingerprint check failed, keeping it:", repr(e)[:200])
    return picked

# ---------- splitting over workers ----------
# ---------- in-container runner (fast mode) ----------
RUNNER = False   # v21: with real deps the runner cost ~19 s CPU for ~5 s simulated (practice)
PROBE = None   # diagnostic builds only
RUNNABLE_PATH = APP / "runner_pinned.json"
_runnable = None
NEVER_RUN = {"tests/test_generator.py::TestGenerator::test_generator_memory"}   # order/memory sensitive

def commit_cost(nums, ms):
    w = split(nums, ms)
    return (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0

def run_or_select(sel, text, nums, ms, id_of, why):
    """Run a small selection in a forked child against the patched code when that is expected to cost
    less CPU than the worker time it saves; then output only the tests that failed."""
    global _runnable
    if _runnable is None:
        _runnable = set(json.loads(RUNNABLE_PATH.read_text())) if RUNNABLE_PATH.exists() else set()
    ids = [id_of[n] for n in nums]
    if not _runnable or any(i not in _runnable or i in NEVER_RUN for i in ids): return nums
    load = sum(ms[n] for n in nums) / 1000
    est = 0.25 + 1.3 * load                     # warm child: fresh sqlglot import + the tests
    saved_if_clean = commit_cost(nums, ms)
    if est >= 0.8 * saved_if_clean: return nums   # a breaking commit pays the CPU and saves little
    files = {}
    for f in parse_patch(text):
        rel = f["new"]
        if not rel or f["old"] != rel or not rel.endswith(".py"): return nums      # fixtures, adds, deletes
        if rel.startswith("sqlglot/"):
            if rel not in sel.files: return nums
            base = sel.files[rel]["text"]
        elif rel.startswith("tests/") and (PINNED_DIR / rel).exists():
            base = (PINNED_DIR / rel).read_text(encoding="utf-8")
        else:
            return nums
        files[rel] = apply(base, f["hunks"])
    import runner, shutil, tempfile
    runner.warm()
    root = Path(tempfile.mkdtemp(prefix="brrun", dir="/tmp"))
    try:
        shutil.copytree(PINNED_DIR, root, dirs_exist_ok=True)     # patched tree on disk: sources, fixtures,
        for rel, src in files.items():                             # subprocesses all see the patched code
            (root / rel).write_text(src, encoding="utf-8")
            pyc = (root / rel).parent / "__pycache__"
            for c in (pyc.glob((root / rel).stem + ".*.pyc") if pyc.exists() else []): c.unlink()
        failed = runner.run({}, ids, timeout=max(5, 4 * est), root=str(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failed is None:
        why.append("runner: error or timeout, selection kept"); return nums
    num_of_id = {id_of[n]: n for n in nums}
    out = {num_of_id[i] for i in failed if i in num_of_id}
    if len(out) != len(failed): return nums                                     # unexpected ids: be safe
    why.append(f"runner: ran {len(nums)} tests ({load:.2f} s recorded, est {est:.2f} s, cpu {runner.LAST_CPU:.2f} s, saved-if-clean {saved_if_clean:.2f} s), {len(out)} failed")
    return out

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
    if sys.argv[1:] == ["--build-pinned"]:
        build_pinned(Selector()); return
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sel = Selector()
    num_of = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            nums, why = sel.select(text)
            if AGGRESSIVE:
                paths = [f["new"] or f["old"] for f in parse_patch(text)]
                if all(paths): nums = aggressive(sel, set(nums), paths, num_of, text)
        except Exception as e:  # never crash: a crash rejects the run, a wide answer only costs time
            nums, why = sel.ALL, [f"error {e!r} -> all"]
        nums = {n for n in nums if n in ms}
        if AGGRESSIVE and RUNNER and nums:
            try:
                nums = run_or_select(sel, text, nums, ms, id_of, why)
            except Exception as e:
                why.append(f"runner error {e!r}: selection kept")
        if PROBE == "no_tpcds": nums = {n for n in nums if id_of[n] != TPCDS_ID}
        elif PROBE == "empty": nums = set()
        print(json.dumps({"commit": patch.stem, "workers": split(nums, ms)}))
        log(patch.stem, len(nums), "tests |", "; ".join(why))

if __name__ == "__main__":
    main()
