"""pytest plugin: per-test line coverage of the repo's tests/ .py files (sys.monitoring)."""
import json, os, sys
import pytest
ROOT = os.getcwd(); TESTS = os.path.join(ROOT, "tests") + os.sep
mon = sys.monitoring; TOOL = 4
cur = ["<collect>"]; data = {}
def _line(code, ln):
    f = code.co_filename
    if not f.startswith(TESTS): return mon.DISABLE
    data.setdefault(cur[0], {}).setdefault(os.path.relpath(f, ROOT), set()).add(ln)
mon.use_tool_id(TOOL, "tcov")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cur[0] = item.nodeid; yield; cur[0] = "<between>"
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    cur[0] = "<class>" + item.nodeid.rsplit("::", 1)[0]; yield; cur[0] = item.nodeid
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    cur[0] = "<class>" + item.nodeid.rsplit("::", 1)[0]; yield; cur[0] = "<between>"
def pytest_sessionfinish(session):
    out = os.environ.get("TCOV_DIR", "/tmp")
    with open(os.path.join(out, f"tcov-{os.getpid()}.json"), "w") as f:
        json.dump({k: {p: sorted(v) for p, v in d.items()} for k, d in data.items()}, f)

# run process pools in-process so pool-side test code (mp_execute, parse_and_optimize) is seen
from concurrent.futures import Future
import pickle as _pickle
def _rt(o): return _pickle.loads(_pickle.dumps(o))
class InlineExecutor:
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
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor
