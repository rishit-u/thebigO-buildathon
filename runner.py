"""In-container test runner (fast mode): run a small candidate selection against the patched code and
output only the tests that actually fail.

Saves the 2 s worker fee (and the tests' load) on commits that break nothing, at the price of the
CPU to run them. Tests are run as plain unittest cases in a forked child that imports the patched
sqlglot (and patched test modules) from memory over the pinned tree in /app/pinned; nothing is
written to disk. Only modules that import without duckdb/pandas/numpy are runnable here; tests that
fail or are unstable on the pinned tree in this environment are never run (see --build).

    python runner.py --build    (image build) -> runner_pinned.json: runnable tests that pass twice
"""
import importlib.abc, importlib.util, json, os, pickle, sys, time, unittest

APP = os.path.dirname(os.path.abspath(__file__))
LAST_CPU = 0.0
ROOT = os.path.join(APP, "pinned")
HEAVY = ("duckdb", "pandas", "numpy", "pytest")


import importlib.machinery as _mach
_AVAILABLE = {m for m in HEAVY if _mach.PathFinder.find_spec(m) is not None}


class _Missing:
    """Any use of a third-party package that the selector image does not have raises."""
    def __init__(self, name): self._n = name
    def __getattr__(self, a): raise RuntimeError(f"{self._n}.{a} is not available in the selector image")
    def __call__(self, *a, **k): raise RuntimeError(f"{self._n} is not available in the selector image")


class _Stub(importlib.abc.Loader):
    def create_module(self, spec): return None
    def exec_module(self, module):
        module.__path__ = []
        module.__getattr__ = lambda a, _n=module.__name__: _Missing(f"{_n}.{a}")


class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serve patched module sources from memory; block heavy third-party packages."""
    def __init__(self, files):
        self.mods = {}
        for rel, src in files.items():
            name = rel[:-3].replace("/", ".")
            if name.endswith(".__init__"): name = name[: -len(".__init__")]
            self.mods[name] = (rel, src)
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in HEAVY and name.split(".")[0] not in _AVAILABLE:
            # not installed in this image: a stand-in that is importable but unusable (tests that use
            # it fail on the pinned tree too, so they are never run here)
            return importlib.util.spec_from_loader(name, _Stub(), is_package=True)
        if name in self.mods:
            rel, _ = self.mods[name]
            return importlib.util.spec_from_loader(name, self, origin=os.path.join(ROOT, rel),
                                                   is_package=rel.endswith("__init__.py"))
        return None
    def create_module(self, spec): return None
    def exec_module(self, module):
        rel, src = self.mods[module.__name__]
        module.__file__ = os.path.join(ROOT, rel)
        if rel.endswith("__init__.py"): module.__path__ = [os.path.dirname(module.__file__)]
        exec(compile(src, module.__file__, "exec"), module.__dict__)


class Result(unittest.TestResult):
    def __init__(self):
        super().__init__(); self.failed = set()
    def addFailure(self, test, err): self.failed.add(getattr(test, "_bb_id", str(test)))
    def addError(self, test, err): self.failed.add(getattr(test, "_bb_id", str(test)))
    def addUnexpectedSuccess(self, test): self.failed.add(getattr(test, "_bb_id", str(test)))
    def addSkip(self, test, reason): self.failed.add(getattr(test, "_bb_id", str(test)))   # not run here: never trust
    def addSubTest(self, test, subtest, err):
        if err is not None: self.failed.add(getattr(test, "_bb_id", str(test)))


class Inline:
    """In-process stand-in for ProcessPoolExecutor, with the pickle round trip a real pool does."""
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*pickle.loads(pickle.dumps(initargs)))
    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        f = Future()
        try: f.set_result(pickle.loads(pickle.dumps(fn(*pickle.loads(pickle.dumps(args)), **kwargs))))
        except BaseException as e: f.set_exception(e)
        return f
    def map(self, fn, *its, **k): return map(fn, *its)
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _child(files, test_ids, root=None):
    """root: a full patched tree on disk (sqlglot/ and tests/), so that tests reading sources or
    fixtures from disk, and subprocesses, see the patched code; otherwise files are overlaid in memory."""
    global ROOT
    os.environ["SKIP_INTEGRATION"] = "0"
    if root: ROOT = root
    os.chdir(ROOT)                                   # tests open fixtures by relative path
    sys.path.insert(0, ROOT)
    sys.meta_path.insert(0, Overlay({} if root else files))
    import unittest as _u                            # noqa: F401  (UNITTEST flag, as under the real suite)
    suite = unittest.TestSuite()
    for tid in test_ids:
        path, cls, meth = tid.split("::")
        mod = importlib.import_module(path[:-3].replace("/", "."))
        if hasattr(mod, "ProcessPoolExecutor"): mod.ProcessPoolExecutor = Inline   # run pooled work in-process
        case = getattr(mod, cls)(meth); case._bb_id = tid
        suite.addTest(case)
    res = Result(); suite.run(res)
    return sorted(res.failed)


_WARM = False
def warm():
    """Import the heavy third-party test dependencies once in the parent: every forked child shares
    them, so a child only pays for importing the (patched) sqlglot and the test modules."""
    global _WARM
    if _WARM: return
    _WARM = True
    for m in ("numpy", "pandas", "pandas.testing", "duckdb"):
        if m.split(".")[0] in _AVAILABLE:
            try: importlib.import_module(m)
            except Exception: pass


def run(files, test_ids, timeout, root=None):
    """Fork a child; return the list of failing test ids, or None on error/timeout."""
    import signal
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            signal.alarm(max(1, int(timeout)))
            out = {"failed": _child(files, test_ids, root)}
        except BaseException as e:
            out = {"error": repr(e)[:300]}
        try: os.write(w, json.dumps(out).encode())
        finally: os._exit(0)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk: break
        data += chunk
    os.close(r)
    _, _, usage = os.wait4(pid, 0)
    global LAST_CPU
    LAST_CPU = usage.ru_utime + usage.ru_stime
    try: out = json.loads(data or b"{}")
    except ValueError: return None
    return out.get("failed") if "failed" in out else None


def build(tests_json):
    """Run every test of the runnable modules on the pinned tree twice; keep those that pass both."""
    ids = [t["id"] for t in json.load(open(tests_json))["tests"]]
    by_mod = {}
    for tid in ids: by_mod.setdefault(tid.split("::")[0], []).append(tid)
    ok = []
    for mod, tids in sorted(by_mod.items()):
        f1 = run({}, tids, 600); f2 = run({}, tids, 600)
        if f1 is None or f2 is None: continue        # module not runnable here
        bad = set(f1) | set(f2)
        other = bad - set(tids)                     # class/module-level errors (setUpClass ...)
        bad_classes = set()
        for o in other:
            import re
            m = re.search(r"\(([\w.]+)\.(\w+)\)", o)
            if not m: bad_classes = None; break     # cannot tell which class: trust none of the module
            bad_classes.add(m.group(2))
        if bad_classes is None: continue
        ok += [t for t in tids if t not in bad and t.split("::")[1] not in bad_classes]
        print(mod, len(tids), "ok", len([t for t in tids if t not in bad]), file=sys.stderr)
    json.dump(sorted(ok), open(os.path.join(APP, "runner_pinned.json"), "w"))


if __name__ == "__main__":
    if sys.argv[1] == "--build": build(sys.argv[2])
