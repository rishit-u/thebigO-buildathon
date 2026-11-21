"""pytest plugin: record which non-Python repo files each test opens (fixture map)."""
import builtins, io, json, os
ROOT = os.getcwd()
_current = ["<collect>"]
_reads = {}
_orig = io.open
def _rec(path):
    try:
        p = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    if p.startswith(ROOT + os.sep) and not p.endswith((".py", ".pyc")):
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
