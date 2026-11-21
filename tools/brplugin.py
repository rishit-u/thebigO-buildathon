"""pytest plugin: record which non-Python repo files each test opens (fixture map)."""
import builtins, io, json, os, sys
ROOT = os.getcwd()
_current = ["<collect>"]
_reads = {}
_orig = io.open
def _rec(path):
    try:
        p = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    if p.startswith(ROOT + os.sep) and not p.endswith(".pyc"):
        _reads.setdefault(_current[0], set()).add(os.path.relpath(p, ROOT))
def _open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        _rec(file)
    return _orig(file, *a, **k)
io.open = _open
builtins.open = _open
import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    # setUpClass / setUpModule run inside the first test's setup and are shared by the
    # whole class, so credit what happens here to the class ("<class>" + its node id).
    _current[0] = "<class>" + item.nodeid.rsplit("::", 1)[0]
    yield
    _current[0] = item.nodeid
def pytest_sessionfinish(session):
    out = os.environ.get("BR_READS_DIR", "/tmp")
    with _orig(os.path.join(out, f"reads-{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _reads.items()}, f)
    # for every test: the source files of every class in the MRO of what it instantiated
    mods = {}
    for ctx, types in _types.items():
        files = set()
        for t in types:
            for c in t.__mro__:
                m = sys.modules.get(c.__module__)
                f = getattr(m, "__file__", None)
                if f and os.path.realpath(f).startswith(ROOT + os.sep):
                    files.add(os.path.relpath(os.path.realpath(f), ROOT))
        mods[ctx] = sorted(files)
    with _orig(os.path.join(out, f"inst-{os.getpid()}.json"), "w") as f:
        json.dump(mods, f)
    _write_attr_reads()


# --- run process pools in-process so coverage sees the work done "in" them ---
from concurrent.futures import Future
import pickle as _pickle
import sys as _sys

def _rt(obj):
    """Pickle round trip, as a real process pool does to arguments and results."""
    return _pickle.loads(_pickle.dumps(obj))

class InlineExecutor:
    """Stand-in for ProcessPoolExecutor: runs each job immediately in this process."""
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

def pytest_collection_finish(session):
    for name, mod in list(_sys.modules.items()):
        if name.startswith("tests") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor


# --- import every sqlglot module before the first test, so code that runs at
# import time is recorded outside any test (context "") instead of being
# credited to whichever test happened to import the module first ---
def pytest_sessionstart(session):
    import importlib, pkgutil, sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    _install_instance_recorder()
    _install_attr_recorders()


# --- which classes each test instantiates (dialects, parsers, generators, expressions...) ---
_types = {}

def _install_instance_recorder():
    from sqlglot.expressions.core import Expr
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.optimizer.scope import Scope
    import sqlglot.schema as schema_mod
    bases = [Expr, Dialect, Parser, Generator, Tokenizer, TypeAnnotator, Scope]
    bases += [c for c in vars(schema_mod).values() if isinstance(c, type) and c.__module__ == schema_mod.__name__ and "__init__" in vars(c)]
    for base in bases:
        orig = base.__init__
        def wrapped(self, *a, __orig=orig, **k):
            _types.setdefault(_current[0], set()).add(type(self))
            return __orig(self, *a, **k)
        base.__init__ = wrapped


# --- scalar class-attribute reads: which tests read (defining class, name) ---
# Installed after every sqlglot module is imported, as non-data descriptors that return the
# original value unchanged. The map builder keeps only attributes that pass its static checks.
_attr_reads = {}
_installed = []   # (class, name, descriptor)

def _is_scalar(v):
    if v is None or isinstance(v, (bool, int, float, str)): return True
    return isinstance(v, tuple) and all(_is_scalar(x) for x in v)

class _ReadRecorder:
    __slots__ = ("value", "key")
    def __init__(self, value, key): self.value = value; self.key = key
    def __get__(self, obj, objtype=None):
        _attr_reads.setdefault(_current[0], set()).add(self.key)
        return self.value

def _install_attr_recorders():
    import inspect as _inspect
    seen = set()
    for modname, mod in list(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")): continue
        for obj in list(vars(mod).values()):
            todo = [obj]
            while todo:
                cls = todo.pop()
                if not isinstance(cls, type) or cls in seen or not cls.__module__.startswith("sqlglot"): continue
                seen.add(cls)
                for name, val in list(vars(cls).items()):
                    if isinstance(val, type): todo.append(val)
                    if name.startswith("__") or not _is_scalar(val): continue
                    key = f"{cls.__module__}:{cls.__qualname__}.{name}"
                    d = _ReadRecorder(val, key)
                    try: setattr(cls, name, d)
                    except (AttributeError, TypeError): continue
                    _installed.append((cls, name, d))

def _write_attr_reads():
    out = os.environ.get("BR_READS_DIR", "/tmp")
    displaced = sorted(d.key for cls, name, d in _installed if vars(cls).get(name) is not d)
    with _orig(os.path.join(out, f"attrs-{os.getpid()}.json"), "w") as f:
        json.dump({"reads": {k: sorted(v) for k, v in _attr_reads.items()},
                   "installed": sorted(d.key for _, _, d in _installed), "displaced": displaced}, f)
