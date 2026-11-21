"""pytest plugin for the map build: value-level facts at every single comparison in sqlglot.

For each `a OP b` site (one operator) it records, per test, the executions where swapping OP for its
"sibling" could give a different answer:
  * `is` <-> `==` and `is not` <-> `!=`: they differ only if (a is b) != (a == b). Safe without computing
    anything when equality is plain identity: a None operand whose partner uses object.__eq__, two members
    of the same Enum class, or two objects whose classes both use object.__eq__. Builtin scalars are
    compared directly. Anything else (e.g. sqlglot expressions, which define __eq__) counts as "may differ".
  * `<` <-> `<=` and `>` <-> `>=`: they differ only if a == b. Computed for builtin scalars (and tuples of
    them); anything else counts as "may differ".
The rewritten code evaluates each operand once, in the original order, and returns the original result.
"""
import ast, builtins, enum, importlib, importlib.abc, importlib.machinery, importlib.util, json, os, pkgutil, sys
import pytest

ROOT = os.getcwd()
_ctx = ["<collect>"]
_eq, _ord = {}, {}          # site -> set of contexts where the sibling operator may differ
SCALARS = (int, float, str, bool, bytes, type(None))
EQ_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}
ORD_OPS = {ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}


def _plain(x):
    """Equality and inequality are both plain identity for this object's class."""
    t = type(x)
    return t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__

_SAFE_ENUM_EQ = {object.__eq__, str.__eq__, int.__eq__}
_SAFE_ENUM_NE = {object.__ne__, str.__ne__, int.__ne__}

def _enum_identity(a, b):
    """Two members of the same Enum class whose equality is not overridden: == and != are identity."""
    t = type(a)
    return (isinstance(a, enum.Enum) and t is type(b)
            and t.__eq__ in _SAFE_ENUM_EQ and t.__ne__ in _SAFE_ENUM_NE)


def _scalar_tuple(x):
    return isinstance(x, tuple) and all(type(e) in SCALARS for e in x)


def _cmp(site, op, a, b):
    if op <= 3:
        if a is None or b is None:
            other = b if a is None else a
            safe = other is None or _plain(other)
        elif _enum_identity(a, b):
            safe = True
        elif _plain(a) and _plain(b):
            safe = True
        elif type(a) in SCALARS and type(b) in SCALARS:
            safe = (a is b) == (a == b) and (a is not b) == (a != b)
        else:
            safe = False
        if not safe:
            _eq.setdefault(site, set()).add(_ctx[0])
        if op == 0: return a is b
        if op == 1: return a is not b
        if op == 2: return a == b
        return a != b
    if (type(a) in SCALARS and type(b) in SCALARS) or (_scalar_tuple(a) and _scalar_tuple(b)):
        try:
            if a == b: _ord.setdefault(site, set()).add(_ctx[0])
        except Exception:
            _ord.setdefault(site, set()).add(_ctx[0])
    else:
        _ord.setdefault(site, set()).add(_ctx[0])
    if op == 4: return a < b
    if op == 5: return a <= b
    if op == 6: return a > b
    return a >= b


builtins.__bb_cmp__ = _cmp

_bools = {}   # site -> {truth tuple: set of contexts}; a tuple has True/False per leaf, None = not evaluated

_PLAIN_BOOL = {object, type(None), bool, int, float, str, bytes, tuple, list, dict, set, frozenset}

def _eval(struct, leaves, truths, flags):
    """Evaluate a BoolOp tree with Python's short-circuit semantics, calling bool() once per evaluated
    leaf (as the and/or itself would) and recording it. A leaf whose class defines its own truthiness
    is flagged, since an extra bool() call by the enclosing `if` could observe state."""
    if isinstance(struct, int):
        v = leaves[struct](); t = type(v)
        if t not in _PLAIN_BOOL and ("__bool__" in vars(t) or "__len__" in vars(t) or
                                     any("__bool__" in vars(c) or "__len__" in vars(c) for c in t.__mro__[1:-1])):
            flags.append(struct)
        truths[struct] = bool(v); return v, truths[struct]
    op, kids = struct
    v = t = None
    for k in kids:
        v, t = _eval(k, leaves, truths, flags)
        if (op == "and" and not t) or (op == "or" and t): return v, t
    return v, t

def _bool(site, struct, *leaves):
    truths = [None] * len(leaves); flags = []
    v, _ = _eval(struct, leaves, truths, flags)
    key = tuple("?" if i in flags else x for i, x in enumerate(truths))   # "?" = custom truthiness: hazard
    _bools.setdefault(site, {}).setdefault(key, set()).add(_ctx[0])
    return v

builtins.__bb_bool__ = _bool

import dis as _dis
_MERGE_OPS = {_dis.opmap.get("DICT_UPDATE"), _dis.opmap.get("DICT_MERGE")} - {None}
_dict_reads = {}     # site -> {key repr: set of contexts} (value read of one key)
_dict_all = {}       # site -> set of contexts (all values read: items/values/copy/|)
_dict_derived = {}   # site -> set of key reprs ("*" = all) read at import outside a dict merge
_dict_flags = {}     # site -> set of {"mutated", "opaque"}
_dict_instances = [] # every recording dict, for identity grouping at the end

def _krepr(k):
    if isinstance(k, type): return k.__name__
    if isinstance(k, enum.Enum): return f"{type(k).__name__}.{k.name}"
    if isinstance(k, (str, int, float, bool, type(None))): return repr(k)
    return None

def _importing(): return _ctx[0] == "<collect>"

def _merge_caller():
    try:
        f = sys._getframe(3)    # _merge_caller <- _value_read <- __getitem__/get <- caller
        return f.f_code.co_code[f.f_lasti] in _MERGE_OPS
    except Exception:
        return False

class _RecDict(dict):
    def _value_read(self, k):
        r = _krepr(k)
        if r is None: _dict_flags.setdefault(self._bb_site, set()).add("opaque"); return
        if _importing():
            if not _merge_caller(): _dict_derived.setdefault(self._bb_site, set()).add(r)
            return
        _dict_reads.setdefault(self._bb_site, {}).setdefault(r, set()).add(_ctx[0])
    def _all_read(self):
        if _importing(): _dict_derived.setdefault(self._bb_site, set()).add("*")
        else: _dict_all.setdefault(self._bb_site, set()).add(_ctx[0])
    def _mutated(self):
        if not _importing(): _dict_flags.setdefault(self._bb_site, set()).add("mutated")
    def __getitem__(self, k):
        self._value_read(k); return dict.__getitem__(self, k)
    def get(self, k, default=None):
        self._value_read(k); return dict.get(self, k, default)
    def __iter__(self): return dict.__iter__(self)          # forces the slow path for {**d} and dict(d)
    def keys(self): return dict.keys(self)
    def items(self): self._all_read(); return dict.items(self)
    def values(self): self._all_read(); return dict.values(self)
    def copy(self): self._all_read(); return dict.copy(self)
    def __or__(self, other): self._all_read(); return dict.__or__(self, other)
    def __ror__(self, other): self._all_read(); return dict.__ror__(self, other)
    def __setitem__(self, k, v): self._mutated(); dict.__setitem__(self, k, v)
    def __delitem__(self, k): self._mutated(); dict.__delitem__(self, k)
    def pop(self, *a): self._mutated(); return dict.pop(self, *a)
    def popitem(self): self._mutated(); return dict.popitem(self)
    def setdefault(self, *a): self._mutated(); return dict.setdefault(self, *a)
    def update(self, *a, **k): self._mutated(); return dict.update(self, *a, **k)
    def clear(self): self._mutated(); return dict.clear(self)
    def __reduce__(self): return (dict, (dict(dict.items(self)),))   # pickles as a plain dict

def _mkdict(site, d):
    r = _RecDict(d); r._bb_site = site; _dict_instances.append(r); return r

builtins.__bb_dict__ = _mkdict


def _flatten(node, leaves):
    """BoolOp tree -> nested (op, [children]) with leaf indices; leaves collected in order."""
    if isinstance(node, ast.BoolOp):
        return ("and" if isinstance(node.op, ast.And) else "or", [_flatten(v, leaves) for v in node.values])
    leaves.append(node); return len(leaves) - 1

def _lambda_safe(node):
    """Leaves become lambdas: skip anything whose meaning a lambda would change."""
    for n in ast.walk(node):
        if isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)): return False
        if isinstance(n, ast.Name) and n.id in ("super", "locals", "vars", "__class__"): return False
    return True

def _struct_ast(struct):
    if isinstance(struct, int): return ast.Constant(struct)
    return ast.Tuple([ast.Constant(struct[0]), ast.Tuple([_struct_ast(k) for k in struct[1]], ast.Load())], ast.Load())

class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel): self.rel = rel; self.depth = 0; self.bool_sites = []; self.dict_sites = []
    def visit_FunctionDef(self, node):
        self.depth += 1; self.generic_visit(node); self.depth -= 1; return node
    def visit_ClassDef(self, node):
        self.generic_visit(node)
        if self.depth == 0:
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if len(tg) == 1 and isinstance(tg[0], ast.Name):
                        site = f"{self.rel}:{st.lineno}"
                        self.dict_sites.append(st.lineno)
                        st.value = ast.copy_location(ast.Call(ast.Name("__bb_dict__", ast.Load()), [ast.Constant(site), st.value], []), st.value)
        return node
    visit_AsyncFunctionDef = visit_FunctionDef
    def _wrap_test(self, test):
        if self.depth == 0 or not isinstance(test, ast.BoolOp) or not _lambda_safe(test): return self.visit(test)
        leaves = []; struct = _flatten(test, leaves)
        site = f"{self.rel}:{test.lineno}:{test.col_offset}"
        self.bool_sites.append(f"{test.lineno}:{test.col_offset}")
        lams = [ast.Lambda(ast.arguments([], [], None, [], [], None, []), self.visit(leaf)) for leaf in leaves]
        call = ast.Call(ast.Name("__bb_bool__", ast.Load()), [ast.Constant(site), _struct_ast(struct)] + lams, [])
        return ast.copy_location(call, test)
    def visit_If(self, node):
        node.test = self._wrap_test(node.test); node.body = [self.visit(n) for n in node.body]
        node.orelse = [self.visit(n) for n in node.orelse]; return node
    def visit_While(self, node):
        node.test = self._wrap_test(node.test); node.body = [self.visit(n) for n in node.body]
        node.orelse = [self.visit(n) for n in node.orelse]; return node
    def visit_IfExp(self, node):
        node.test = self._wrap_test(node.test); node.body = self.visit(node.body); node.orelse = self.visit(node.orelse); return node
    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1: return node
        code = EQ_OPS.get(type(node.ops[0]), ORD_OPS.get(type(node.ops[0])))
        if code is None: return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name("__bb_cmp__", ast.Load()),
                        args=[ast.Constant(site), ast.Constant(code), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")): return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not (spec.origin or "").endswith(".py"): return spec
        spec.loader = _Loader(spec.origin)
        return spec


class _Loader(importlib.abc.Loader):
    def __init__(self, origin): self.origin = origin
    def create_module(self, spec): return None
    def exec_module(self, module):
        src = open(self.origin, encoding="utf-8").read()
        rel = os.path.relpath(os.path.realpath(self.origin), ROOT)
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(ast.parse(src)))
        exec(compile(tree, self.origin, "exec"), module.__dict__)


sys.meta_path.insert(0, _Finder())


def pytest_sessionstart(session):
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"): continue
        try: importlib.import_module(info.name)
        except Exception: pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _ctx[0] = item.nodeid
    yield
    _ctx[0] = "<between>"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _ctx[0] = "<class>" + item.nodeid.rsplit("::", 1)[0]
    yield
    _ctx[0] = item.nodeid


import pickle
from concurrent.futures import Future

def _rt(obj): return pickle.loads(pickle.dumps(obj))

class _Inline:
    """Same in-process stand-in for ProcessPoolExecutor as brplugin (pickle round trip included)."""
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*_rt(initargs))
    def submit(self, fn, *args, **kwargs):
        f = Future()
        try: f.set_result(_rt(fn(*_rt(args), **_rt(kwargs))))
        except BaseException as e: f.set_exception(e)
        return f
    def map(self, fn, *its, **k): return map(fn, *its)
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

class _AlwaysMiss(dict):
    def get(self, key, default=None): return default

class _NeverStore(_AlwaysMiss):
    def __setitem__(self, key, value): pass

def pytest_collection_finish(session):
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = _Inline
    # same cache handling as brplugin, so cached work is profiled for every test that uses it
    import sqlglot, sqlglot.generator as gen, sqlglot.schema as sch
    gen._DISPATCH_CACHE = _NeverStore()
    orig = sch.MappingSchema.__init__
    def init(self, *a, __orig=orig, **k):
        __orig(self, *a, **k)
        for n in ("_find_cache", "_normalized_table_cache", "_normalized_name_cache"):
            if isinstance(getattr(self, n, None), dict): setattr(self, n, _AlwaysMiss(getattr(self, n)))
    sch.MappingSchema.__init__ = init
    obj = getattr(sqlglot, "schema", None)
    if isinstance(obj, sch.MappingSchema):
        for n in ("_find_cache", "_normalized_table_cache", "_normalized_name_cache"):
            if isinstance(getattr(obj, n, None), dict): setattr(obj, n, _AlwaysMiss(getattr(obj, n)))


def _identity_groups():
    """Entries that hold the very same value object (copied by merges): [[site, key repr], ...]."""
    by_id = {}
    for d in _dict_instances:
        for k, v in dict.items(d):
            r = _krepr(k)
            if r is not None: by_id.setdefault((id(v), r), set()).add((d._bb_site, r))
    return [sorted(map(list, g)) for g in by_id.values() if len(g) > 1]

def pytest_sessionfinish(session):
    out = os.environ.get("BR_READS_DIR", "/tmp")
    with open(os.path.join(out, f"valprof-{os.getpid()}.json"), "w") as f:
        json.dump({"eq": {k: sorted(v) for k, v in _eq.items()}, "ord": {k: sorted(v) for k, v in _ord.items()},
                   "bool": {k: [[list(t), sorted(c)] for t, c in d.items()] for k, d in _bools.items()},
                   "dict_reads": {s_: {k: sorted(c) for k, c in d.items()} for s_, d in _dict_reads.items()},
                   "dict_all": {s_: sorted(c) for s_, c in _dict_all.items()},
                   "dict_derived": {s_: sorted(v) for s_, v in _dict_derived.items()},
                   "dict_flags": {s_: sorted(v) for s_, v in _dict_flags.items()},
                   "dict_groups": _identity_groups()}, f)
