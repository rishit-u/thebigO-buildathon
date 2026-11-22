import ast
BIN = {ast.Add: "-", ast.Sub: "+", ast.Mult: "+", ast.FloorDiv: "/"}
def mutate(src, op, rng):
    lines = src.split("\n"); tree = ast.parse(src)
    nodes = [n for n in ast.walk(tree) if hasattr(n, "lineno") and n.lineno == getattr(n, "end_lineno", -1)]
    rng.shuffle(nodes)
    for n in nodes:
        l = lines[n.lineno - 1]; seg = l[n.col_offset:n.end_col_offset]; new = None
        if op == "str" and isinstance(n, ast.Constant) and isinstance(n.value, str) and 2 < len(seg) < 60 and seg[0] in "\"'" and not seg.startswith(('"""', "'''")) and "\\" not in seg:
            i = rng.randrange(1, len(seg) - 1); new = seg[:i] + rng.choice(["X", "_", " "]) + seg[i:]
        elif op == "int" and isinstance(n, ast.Constant) and type(n.value) is int and seg.isdigit():
            new = str(n.value + rng.choice([1, -1]))
        elif op == "arith" and isinstance(n, ast.BinOp) and type(n.op) in BIN:
            a = l[n.left.end_col_offset:n.right.col_offset] if n.left.end_lineno == n.lineno else None
            sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//"}[type(n.op)]
            if a and a.count(sym) == 1:
                new = seg.replace(a, a.replace(sym, BIN[type(n.op)]), 1) if False else l[n.col_offset:n.left.end_col_offset] + a.replace(sym, BIN[type(n.op)]) + l[n.right.col_offset:n.end_col_offset]
        elif op == "not" and isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and seg.startswith("not "):
            new = seg[4:]
        elif op == "return_none" and isinstance(n, ast.Return) and n.value is not None and not seg.endswith(("(", "[", "{", ",")):
            new = "return None"
        elif op == "del_stmt" and isinstance(n, (ast.Expr, ast.Assign, ast.AugAssign)) and isinstance(getattr(n, "value", None), (ast.Call, ast.BinOp, ast.Name, ast.Attribute)) and seg == l.strip():
            new = "pass"
        elif op == "kwarg_bool" and isinstance(n, ast.keyword) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, bool):
            new = seg.replace(repr(n.value.value), repr(not n.value.value))
        elif op == "dict_del" and isinstance(n, ast.Dict) and False: pass
        elif op == "cmp" and isinstance(n, ast.Compare) and len(n.ops) == 1:
            sw = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.Gt: "<=", ast.LtE: ">", ast.GtE: "<", ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is"}
            mid = l[n.left.end_col_offset:n.comparators[0].col_offset] if n.left.end_lineno == n.lineno else ""
            o = mid.strip()
            if o in ("==", "!=", "<", ">", "<=", ">=", "in", "not in", "is", "is not"):
                new = l[n.col_offset:n.left.end_col_offset] + " " + sw[type(n.ops[0])] + " " + l[n.comparators[0].col_offset:n.end_col_offset]
        elif op == "method" and isinstance(n, ast.Attribute) and n.attr in ("upper", "lower", "lstrip", "rstrip", "startswith", "endswith", "append", "extend"):
            alt = {"upper": "lower", "lower": "upper", "lstrip": "rstrip", "rstrip": "lstrip", "startswith": "endswith", "endswith": "startswith", "append": "extend", "extend": "append"}
            if seg.endswith("." + n.attr): new = seg[: -len(n.attr)] + alt[n.attr]
        if new is None or new == seg: continue
        lines[n.lineno - 1] = l[:n.col_offset] + new + l[n.end_col_offset:]
        out = "\n".join(lines)
        try: ast.parse(out)
        except SyntaxError: lines[n.lineno - 1] = l; continue
        return out
    return None
