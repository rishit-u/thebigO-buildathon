"""Digest of the state sqlglot builds at import time.

Imports every sqlglot module from <root> (in a fixed order, PYTHONHASHSEED fixed by the caller), then
walks every module's globals and, recursively, every value reachable from them: containers, instances,
sqlglot classes' __dict__, functions' defaults and closure cells. Code objects are not compared, so an
edited function body only changes the digest through what running it at import produced.

Usage: python -I statedigest.py <dir containing sqlglot/>    -> prints the hex digest
"""
import enum, hashlib, importlib, pkgutil, re, sys, types


def main():
    root = sys.argv[1]
    sys.path.insert(0, root)
    sys.setrecursionlimit(200000)
    import unittest  # noqa: F401  -- the suite runs with UNITTEST on (expressions/core.py)
    import sqlglot
    names = ["sqlglot"]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if not info.name.endswith("__main__"):
            names.append(info.name)
    h = hashlib.blake2b(digest_size=20)
    dump = open(sys.argv[2], "w") if len(sys.argv) > 2 else None
    def out(s):
        h.update(s.encode("utf-8", "surrogatepass")); h.update(b"\x00")
        if dump: dump.write(s.replace("\n", "\\n") + "\n")
    mods = []
    for n in sorted(names):
        try:
            mods.append(importlib.import_module(n))
        except Exception as e:
            out(f"ERR {n} {type(e).__name__}")

    def own(obj):
        m = getattr(obj, "__module__", None) or ""
        return m == "sqlglot" or m.startswith("sqlglot.")

    def skey(x):
        """A run-independent sort key for set elements (classes and objects hash by id)."""
        if x is None or isinstance(x, (bool, int, float, str, bytes)): return (0, type(x).__name__, repr(x))
        if isinstance(x, enum.Enum): return (1, type(x).__qualname__, x.name)
        if isinstance(x, (type, types.FunctionType)): return (2, x.__module__ or "", x.__qualname__)
        if isinstance(x, tuple): return (3, tuple(skey(y) for y in x))
        r = repr(x)
        return (4, type(x).__qualname__, "" if " at 0x" in r else r)

    seen = {}
    def ser(v, top=False):
        if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
            out(type(v).__name__ + ":" + repr(v)); return
        if isinstance(v, enum.Enum):
            out(f"enum:{type(v).__module__}.{type(v).__qualname__}.{v.name}"); return
        if isinstance(v, types.ModuleType):
            out("module:" + v.__name__); return
        if isinstance(v, type):
            out(f"class:{v.__module__}.{v.__qualname__}")
            if top and own(v):                     # a class's contents: once, where its module defines it
                ser(v.__bases__)
                for a, b in sorted(v.__dict__.items()):
                    if a in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"): continue
                    out(a); ser(b)
                    if isinstance(b, type) and b.__qualname__.startswith(v.__qualname__ + "."): ser(b, True)
            return
        k = id(v)
        if k in seen:
            out(f"@{seen[k]}"); return             # shared object: by first-visit index (fixed hash seed)
        seen[k] = len(seen)
        ser_inner(v)

    def ser_inner(v):
        t = type(v)
        if t in (tuple, list):
            out(f"{t.__name__}[{len(v)}")
            if v and all(isinstance(x, type) for x in v):   # built from a set of classes: order follows id()
                v = sorted(v, key=skey)
            for x in v: ser(x)
            out("]"); return
        if t in (set, frozenset):
            out(f"{t.__name__}{{{len(v)}")
            for x in sorted(v, key=skey): ser(x)
            out("}"); return
        if isinstance(v, dict):
            out(f"dict:{t.__module__}.{t.__qualname__}{{{len(v)}")
            for a, b in sorted(v.items(), key=lambda ab: skey(ab[0])): ser(a); ser(b)
            out("}")
            if t is not dict: ser_attrs(v)
            return
        if isinstance(v, (staticmethod, classmethod)):
            out(t.__name__); ser(v.__func__); return
        if isinstance(v, property):
            out("property"); ser(v.fget); ser(v.fset); ser(v.fdel); return
        if isinstance(v, types.MethodType):
            out("method"); ser(v.__func__); ser(v.__self__); return
        if isinstance(v, types.FunctionType):
            out(f"fn:{v.__module__}.{v.__qualname__}")
            ser(v.__defaults__); ser(v.__kwdefaults__)
            for c in (v.__closure__ or ()):
                try: ser(c.cell_contents)
                except ValueError: out("emptycell")
            return
        if isinstance(v, (types.BuiltinFunctionType, types.MethodWrapperType, types.WrapperDescriptorType,
                          types.MethodDescriptorType, types.GetSetDescriptorType, types.MemberDescriptorType)):
            out(f"builtin:{getattr(v, '__qualname__', repr(t))}"); return
        if isinstance(v, re.Pattern):
            out(f"re:{v.pattern!r}:{v.flags}"); return
        out(f"obj:{t.__module__}.{t.__qualname__}")
        ser_attrs(v)

    def ser_attrs(v):
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            for a, b in d.items(): out(a); ser(b)
        for c in type(v).__mro__:
            for s in getattr(c, "__slots__", ()) if isinstance(getattr(c, "__slots__", ()), (tuple, list)) else ():
                if s in ("__dict__", "__weakref__"): continue
                try: b = object.__getattribute__(v, s)
                except AttributeError: out(f"{s}=unset"); continue
                out(s); ser(b)

    for m in mods:
        out("MODULE " + m.__name__)
        for a, b in list(vars(m).items()):
            if a in ("__builtins__", "__file__", "__cached__", "__spec__", "__loader__", "__path__", "__doc__"): continue
            out(a)
            ser(b, top=isinstance(b, type) and b.__module__ == m.__name__ and b.__qualname__ == a)
    print(h.hexdigest())


if __name__ == "__main__":
    main()
