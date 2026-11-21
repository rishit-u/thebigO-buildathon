"""TPC-DS result-level check (fast mode), extending tpcds_fp.py.

For every query, compute the same stage hashes as tpcds_fp.py on the patched tree. Where they differ
from the pinned ones: a query the test does not execute only needs to parse without error; for an
executed query the DuckDB SQL (the expected side) must be unchanged, and then the patched executor is
run on that query with the pinned TPC-DS tables and its rows compared with the pinned executor rows.
All equal: the test's comparisons see exactly what they saw on the pinned commit.

Usage: python -I tpcds_res.py <dir with sqlglot/> <tpcds_spec.json> <pinned stage hashes> <pinned results> <data dir> [--build]
  --build: print the pinned results ({query: digest}) instead of checking.
"""
import csv, enum, gzip, hashlib, json, math, pickle, sys, time
import ast as _ast


def main():
    root, spec = sys.argv[1], json.load(open(sys.argv[2]))
    pinned = json.load(open(sys.argv[3])); pinned_res_path = sys.argv[4]; data = sys.argv[5]
    build = "--build" in sys.argv

    sys.path.insert(0, root)
    import unittest  # noqa: F401  -- the test suite runs with UNITTEST on (expressions/core.py)
    import sqlglot
    from sqlglot import exp, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan, Step
    from sqlglot.schema import ensure_schema

    def h(*parts):
        m = hashlib.blake2b(digest_size=12)
        for p in parts: m.update(p.encode() if isinstance(p, str) else p); m.update(b"\x00")
        return m.hexdigest()

    # Serialize every value into one token list per stage and hash it once (fast, and exact: the
    # token stream is unambiguous because every container is bracketed with its type and length).
    Expr = exp.Expr
    def ser(v, out):
        if isinstance(v, Expr):
            out.append("E" + type(v).__module__ + "." + type(v).__qualname__)
            args = v.args
            out.append(str(len(args)))
            for key in sorted(args):
                out.append(key); ser(args[key], out)
            ser(getattr(v, "_type", None), out); ser(v.comments, out)
            return
        t = type(v)
        if v is None or t is bool or t is int or t is str: out.append(t.__name__[0] + repr(v)); return
        if t is float: out.append("f" + ("inf" if math.isinf(v) else repr(v))); return
        if isinstance(v, Step): out.append("S" + step_hash(v)); return
        if isinstance(v, type): out.append("T" + v.__module__ + "." + v.__qualname__); return
        if isinstance(v, enum.Enum): out.append("N" + type(v).__qualname__ + "." + v.name); return
        if isinstance(v, (list, tuple)):
            out.append(t.__name__[0] + str(len(v)))
            for x in v: ser(x, out)
            return
        if isinstance(v, (set, frozenset)):
            parts = []
            for x in v:
                o = []; ser(x, o); parts.append("\x01".join(o))
            out.append("s" + str(len(v))); out.extend(sorted(parts)); return
        if isinstance(v, dict):
            out.append("d" + str(len(v)))
            for k, x in v.items(): ser(k, out); ser(x, out)
            return
        raise TypeError(f"unhashable value of type {t}")   # caller treats as mismatch

    def digest(out):
        return hashlib.blake2b("\x00".join(out).encode(), digest_size=12).hexdigest()
    def vh(v):
        o = []; ser(v, o); return digest(o)
    eh = vh
    smemo = {}
    def step_hash(st):
        k = id(st)
        if k not in smemo:
            o = [type(st).__qualname__]
            for key in sorted(vars(st)):
                if key == "dependents": continue      # back edges: covered from the other side
                o.append(key); ser(getattr(st, key), o)
            smemo[k] = (st, digest(o))
        return smemo[k][1]
    sh = step_hash
    def h(*parts): return digest(list(parts))
    class _M:
        def clear(self): pass
    memo = _M()

    schema = spec["schema"]
    from sqlglot import find_tables
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    tables = None
    def load_tables():
        out = {}
        for table, columns in schema.items():
            reader = csv.reader(gzip.open(f"{data}/{table}.csv.gz", "rt", newline=""), delimiter="|")
            next(reader); rows = []; ctypes = []
            for row in reader:
                if not ctypes:
                    for v in row:
                        try: ctypes.append(type(_ast.literal_eval(v)))
                        except (ValueError, SyntaxError): ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            out[table] = Table(columns=columns, rows=rows)
        return out
    def result_digest(e):
        tb = {t.name: tables[t.name] for t in find_tables(e)}
        r = execute(e, schema=schema, tables=tb)
        return hashlib.blake2b(repr((list(r.columns), list(r.rows))).encode(), digest_size=16).hexdigest()
    if build:
        tables = load_tables(); out = {}
        for i, (execute_flag, sql) in enumerate(spec["queries"]):
            if execute_flag: out[str(i)] = result_digest(pickle.loads(pickle.dumps(parse_one(sql))))
        json.dump(out, sys.stdout); return
    pinned_res = json.load(open(pinned_res_path))
    budget = 6.0; spent = 0.0; ran = 0
    for i, (execute_flag, sql) in enumerate(spec["queries"]):
        try:
            e = parse_one(sql)
            row = [eh(e)]
            e2 = pickle.loads(pickle.dumps(e))
            row.append(eh(e2))
            row.append(h(*transpile(sql, write="duckdb")))
            if execute_flag:
                o = optimize(e2, ensure_schema(schema), leave_tables_isolated=True)
                row.append(eh(o))
                p = Plan(o)
                row += [eh(p.expression), vh(p.ctes), sh(p.root)]
        except Exception as ex:
            json.dump({"ok": False, "why": f"query {i}: {type(ex).__name__}"}, sys.stdout); return
        smemo.clear()
        if pinned.get(str(i)) == row: continue
        if not execute_flag: continue                      # only parsed by the test: no error is enough
        if row[2] != pinned[str(i)][2]:
            json.dump({"ok": False, "why": f"query {i}: DuckDB SQL changed"}, sys.stdout); return
        if tables is None: tables = load_tables()
        t0 = time.process_time()
        try:
            d = result_digest(pickle.loads(pickle.dumps(parse_one(sql))))
        except Exception as ex:
            json.dump({"ok": False, "why": f"query {i}: executor {type(ex).__name__}"}, sys.stdout); return
        spent += time.process_time() - t0; ran += 1
        if d != pinned_res.get(str(i)):
            json.dump({"ok": False, "why": f"query {i}: result changed"}, sys.stdout); return
        if spent > budget:
            json.dump({"ok": False, "why": "budget"}, sys.stdout); return
    json.dump({"ok": True, "ran": ran}, sys.stdout)


if __name__ == "__main__":
    main()
