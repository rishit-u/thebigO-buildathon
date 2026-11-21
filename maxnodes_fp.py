"""Behavioural fingerprint of the parser on scaled-down test_max_nodes inputs (fast mode).

test_max_nodes (28.8 s) parses SELECT lists and long `x=x=...` chains and checks the AST-node limit
error. This script runs the same kinds of input at small scale on a given sqlglot tree and prints, for
each case, a hash of the resulting tree or the exception type and message. Equal output for the
patched and pinned trees means the parser behaves the same on these inputs, node counting and limit
errors included (several limits around the exact node count are probed for off-by-one changes).

Usage: python -I maxnodes_fp.py <dir containing sqlglot/>   -> prints JSON list
"""
import hashlib, sys


def main():
    sys.path.insert(0, sys.argv[1])
    import unittest  # noqa: F401  -- the test suite runs with UNITTEST on (expressions/core.py)
    from sqlglot import exp, parse_one

    def tree_hash(e):
        out = []
        def ser(v):
            if isinstance(v, exp.Expr):
                out.append("E" + type(v).__qualname__)
                for k in sorted(v.args): out.append(k); ser(v.args[k])
                out.append("/")
            elif isinstance(v, list):
                out.append("L%d" % len(v))
                for x in v: ser(x)
            else:
                out.append(type(v).__name__ + repr(v))
        stack_safe = sys.getrecursionlimit()
        sys.setrecursionlimit(max(stack_safe, 20000))
        ser(e)
        return hashlib.blake2b("\x00".join(out).encode(), digest_size=12).hexdigest()

    def run(sql, **kw):
        try:
            e = parse_one(sql, **kw)
            return ["ok", type(e).__qualname__, tree_hash(e)]
        except Exception as ex:
            return ["error", type(ex).__qualname__, str(ex)[:300]]

    cases = []
    cases.append(run("SELECT 1", max_nodes=1_000_000))
    cases.append(run("SELECT " + ",".join(f"col{i}" for i in range(500)) + " FROM t", max_nodes=1_000_000))
    for width, depth in ((20, 100), (200, 30), (60, 100)):
        sql = "SELECT " + ",".join("x" + "=x" * depth for _ in range(width))
        cases.append(run(sql))                            # no limit: the whole tree
        cases.append(run(sql, max_nodes=1_000_000))       # limit far above
        for lim in (5, 50, 500, 5000):
            cases.append(run(sql, max_nodes=lim))         # limit hit at different points
    # find the exact node count of one input and probe limits around it
    sql = "SELECT " + ",".join("x" + "=x" * 10 for _ in range(10))
    lo, hi = 1, 4096
    while lo < hi:                                         # smallest limit that parses
        mid = (lo + hi) // 2
        if run(sql, max_nodes=mid)[0] == "ok": hi = mid
        else: lo = mid + 1
    cases.append(["threshold", lo])
    for lim in (lo - 1, lo, lo + 1):
        cases.append(run(sql, max_nodes=lim))
    cases.append(run("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5))
    cases.append(run("SELECT 1, 2, 3"))
    import json
    json.dump(cases, sys.stdout)


if __name__ == "__main__":
    main()
