"""TPC-DS stage fingerprints (mechanism 1A).

test_execute_tpcds, per query: parse_one (main process) -> pickle -> optimize -> Plan ->
PythonExecutor (worker), compared with DuckDB running transpile(sql, write="duckdb").
This script computes, for a sqlglot tree given on the command line, a structural hash of every
value that flows out of the parse / DuckDB-SQL / optimize / plan stages. If a patched tree gives the
same hashes as the pinned one, and no code that runs outside those stages changed, the Python
execution sees identical inputs and runs identical code, so the test outcome cannot change.

Usage: python -I tpcds_fp.py <dir containing sqlglot/> <tpcds.json>   -> prints JSON {query: [h...]}
tpcds.json: {"queries": [[execute_flag, sql], ...], "schema": {...}} (from the pinned fixtures)
"""
import enum, hashlib, json, math, pickle, sys


def main():
    root, spec = sys.argv[1], json.load(open(sys.argv[2]))
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
    pinned = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None   # stop at the first difference
    out = {}
    for i, (execute, sql) in enumerate(spec["queries"]):
        try:
            e = parse_one(sql)
            row = [eh(e)]
            e2 = pickle.loads(pickle.dumps(e))     # what the worker receives
            row.append(eh(e2))
            row.append(h(*transpile(sql, write="duckdb")))
            if execute:
                o = optimize(e2, ensure_schema(schema), leave_tables_isolated=True)
                row.append(eh(o))
                p = Plan(o)
                row += [eh(p.expression), vh(p.ctes), sh(p.root)]
        except Exception as ex:   # an error is a result too: same error, same outcome
            row = ["error", type(ex).__name__, str(ex)[:200]]
        out[str(i)] = row
        memo.clear(); smemo.clear()
        if pinned is not None and pinned.get(str(i)) != row:
            json.dump({"mismatch": i}, sys.stdout); return
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
