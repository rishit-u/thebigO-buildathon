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
    import sqlglot
    from sqlglot import exp, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan, Step
    from sqlglot.schema import ensure_schema

    def h(*parts):
        m = hashlib.blake2b(digest_size=12)
        for p in parts: m.update(p.encode() if isinstance(p, str) else p); m.update(b"\x00")
        return m.hexdigest()

    memo = {}
    def eh(e):
        """Structural hash of an expression: class, every arg, the annotated type, comments."""
        k = id(e)
        if k in memo: return memo[k][1]
        parts = [type(e).__module__ + "." + type(e).__qualname__]
        for key in sorted(e.args):
            parts += [key, vh(e.args[key])]
        parts += ["_type", vh(getattr(e, "_type", None)), "comments", vh(e.comments)]
        r = h(*parts); memo[k] = (e, r); return r

    smemo = {}
    def vh(v):
        if isinstance(v, exp.Expr): return eh(v)
        if isinstance(v, Step): return sh(v)
        if v is None or isinstance(v, (bool, int, str)): return h(type(v).__name__, repr(v))
        if isinstance(v, float): return h("float", "inf" if math.isinf(v) else repr(v))
        if isinstance(v, type): return h("type", v.__module__ + "." + v.__qualname__)
        if isinstance(v, enum.Enum): return h("enum", type(v).__module__ + "." + type(v).__qualname__, v.name)
        if isinstance(v, (list, tuple)): return h(type(v).__name__, *[vh(x) for x in v])
        if isinstance(v, (set, frozenset)): return h("set", *sorted(vh(x) for x in v))
        if isinstance(v, dict): return h("dict", *[vh(k) + vh(x) for k, x in v.items()])
        raise TypeError(f"unhashable value of type {type(v)}")   # caller treats as mismatch

    def sh(s):
        k = id(s)
        if k in smemo: return smemo[k][1]
        parts = [type(s).__qualname__]
        for key in sorted(vars(s)):
            if key == "dependents": continue      # back edges: covered from the other side
            parts += [key, vh(getattr(s, key))]
        r = h(*parts); smemo[k] = (s, r); return r

    schema = spec["schema"]
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
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
