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
    return type(x).__eq__ is object.__eq__


def _scalar_tuple(x):
    return isinstance(x, tuple) and all(type(e) in SCALARS for e in x)


def _cmp(site, op, a, b):
    if op <= 3:
        if a is None or b is None:
            other = b if a is None else a
            safe = other is None or _plain(other)
        elif isinstance(a, enum.Enum) and type(a) is type(b):
            safe = True
        elif _plain(a) and _plain(b):
            safe = True
        elif type(a) in SCALARS and type(b) in SCALARS:
            safe = (a is b) == (a == b)
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


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel): self.rel = rel
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


def pytest_sessionfinish(session):
    out = os.environ.get("BR_READS_DIR", "/tmp")
    with open(os.path.join(out, f"valprof-{os.getpid()}.json"), "w") as f:
        json.dump({"eq": {k: sorted(v) for k, v in _eq.items()}, "ord": {k: sorted(v) for k, v in _ord.items()}}, f)
