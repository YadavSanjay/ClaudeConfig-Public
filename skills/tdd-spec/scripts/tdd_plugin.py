"""pytest plugin for the controlled run (`-p tdd_plugin`; plugin autoload is off).

Report: when $TDD_REPORT is set, records the collected node ids and each test's outcome and exact
exception (type, string args, group members, assertion or not) as JSON for the classifier.
Variants (red_check pass 2): when $TDD_VARIANT and $TDD_INTERFACE are set, switches every interface
item ({"module", "qualname"}) to the same wrong-value variant before test modules are imported, so
`from pkg.mod import f` in a test also sees it. An item with no value for the variant returns None.
"""

from __future__ import annotations

import dataclasses
import decimal
import fractions
import importlib
import json
import math
import os
import types
import typing
from builtins import BaseExceptionGroup
from typing import Any, Callable

import pytest


# =========================================================================== tdd_report
_collected: list[str] = []
_results: dict[str, dict[str, Any]] = {}


def _exc_info(e: BaseException, depth: int = 0) -> dict[str, Any]:
    t = type(e)
    info: dict[str, Any] = {"type": f"{t.__module__}.{t.__qualname__}",
                            "args": [a if isinstance(a, str) else repr(a) for a in e.args],
                            # pytest's Failed reports its module as "builtins"; decide by isinstance
                            "assertion": isinstance(e, (AssertionError, pytest.fail.Exception))}
    if isinstance(e, BaseExceptionGroup) and depth < 5:
        info["members"] = [_exc_info(m, depth + 1) for m in e.exceptions]
    return info


def pytest_collection_finish(session: pytest.Session) -> None:
    _collected[:] = [item.nodeid for item in session.items]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    outcome = yield
    rep = outcome.get_result()
    prev = _results.get(item.nodeid)
    if rep.when == "call":
        rec: dict[str, Any] = {"outcome": rep.outcome, "when": "call"}
        if hasattr(rep, "wasxfail"):
            rec["outcome"] = "xpassed" if rep.passed else "xfailed"
        if rep.failed and call.excinfo is not None:
            rec["exc"] = _exc_info(call.excinfo.value)
            rec["longrepr"] = str(rep.longrepr)[-6000:]
        _results[item.nodeid] = rec
    elif rep.failed:                                     # error in setup or teardown
        _results[item.nodeid] = {"outcome": "error", "when": rep.when,
                                 "longrepr": str(rep.longrepr)[-6000:]}
    elif rep.skipped and prev is None:
        _results[item.nodeid] = {"outcome": "skipped", "when": rep.when}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = os.environ.get("TDD_REPORT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"collected": _collected, "results": _results, "exitstatus": int(exitstatus)}, f)


# =========================================================================== tdd_variants
_MISSING = object()
_NUMERIC = (bool, int, float, complex, decimal.Decimal, fractions.Fraction)
_CONTAINERS = (list, tuple, dict, set, frozenset)


def _strip_optional(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return args[0] if args else type(None)
    return tp


def _is_numeric(tp: Any) -> bool:
    if isinstance(tp, type) and issubclass(tp, _NUMERIC):
        return True
    return isinstance(tp, type) and any(c.__module__ == "numpy" and c.__name__ in ("number", "bool_")
                                        for c in tp.__mro__)


def _empty_of(tp: Any) -> Any:
    origin = typing.get_origin(tp) or tp
    if isinstance(origin, type) and issubclass(origin, _CONTAINERS):
        return origin()
    if isinstance(tp, type):
        if tp.__name__ in ("DataFrame", "Series") and tp.__module__.startswith("pandas"):
            return tp()
        if tp.__name__ == "ndarray" and tp.__module__ == "numpy":
            import numpy  # noqa: PLC0415
            return numpy.empty(0)
    return _MISSING


def _zero_of(tp: Any, depth: int) -> Any:
    tp = _strip_optional(tp)
    if _is_numeric(tp):
        return tp(0)
    if tp is str:
        return ""
    if (empty := _empty_of(tp)) is not _MISSING:
        return empty
    if depth < 2 and (inst := _zero_instance(tp, depth + 1)) is not _MISSING:
        return inst
    return None


def _zero_instance(tp: Any, depth: int = 0) -> Any:
    if not isinstance(tp, type):
        return _MISSING
    try:
        hints = typing.get_type_hints(tp)
    except Exception:  # noqa: BLE001
        hints = {}
    if dataclasses.is_dataclass(tp):
        values = {f.name: _zero_of(hints.get(f.name, Any), depth) for f in dataclasses.fields(tp)}
        try:
            return tp(**{k: v for k, v in values.items()
                         if next(f for f in dataclasses.fields(tp) if f.name == k).init})
        except Exception:  # noqa: BLE001
            obj = object.__new__(tp)
            for k, v in values.items():
                object.__setattr__(obj, k, v)
            return obj
    if hasattr(tp, "model_construct") and hasattr(tp, "model_fields"):        # pydantic v2
        return tp.model_construct(**{k: _zero_of(hints.get(k, Any), depth) for k in tp.model_fields})
    return _MISSING


def value_for(return_hint: Any, variant: str) -> Callable[[], Any]:
    """A factory for the wrong value an item returns under `variant`."""
    if return_hint is _MISSING or return_hint is None or return_hint is type(None):
        return lambda: None
    tp = _strip_optional(return_hint)
    if variant == "zero" and _is_numeric(tp):
        return lambda: tp(0)
    if variant == "nan" and _is_numeric(tp):
        return lambda: math.nan
    if variant == "empty":
        if tp is str:
            return lambda: ""
        if _empty_of(tp) is not _MISSING:
            return lambda: _empty_of(tp)
    if variant == "zero_instance" and _zero_instance(tp) is not _MISSING:
        return lambda: _zero_instance(tp)
    return lambda: None


def applicable(return_hint: Any, variant: str) -> bool:
    """Whether `variant` gives this item something other than None."""
    if variant == "none":
        return True
    try:
        return value_for(return_hint, variant)() is not None
    except Exception:  # noqa: BLE001
        return False


def resolve(module: str, qualname: str) -> tuple[Any, str, Any]:
    owner: Any = importlib.import_module(module)
    *parents, name = qualname.split(".")
    for p in parents:
        owner = getattr(owner, p)
    raw = owner.__dict__[name] if isinstance(owner, type) else getattr(owner, name)
    return owner, name, raw


def return_hint(raw: Any) -> Any:
    fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
    try:
        return typing.get_type_hints(fn).get("return", _MISSING)
    except Exception:  # noqa: BLE001
        return getattr(fn, "__annotations__", {}).get("return", _MISSING)


def patch_all(items: list[dict[str, str]], variant: str) -> None:
    for it in items:
        owner, name, raw = resolve(it["module"], it["qualname"])
        factory = value_for(return_hint(raw), variant)

        def wrong(*_a: Any, _f: Callable[[], Any] = factory, **_k: Any) -> Any:
            return _f()

        if isinstance(raw, staticmethod):
            setattr(owner, name, staticmethod(wrong))
        elif isinstance(raw, classmethod):
            setattr(owner, name, classmethod(wrong))
        else:
            setattr(owner, name, wrong)


def pytest_configure(config: Any) -> None:
    variant = os.environ.get("TDD_VARIANT")
    path = os.environ.get("TDD_INTERFACE")
    if variant and path:
        with open(path, encoding="utf-8") as f:
            patch_all(json.load(f), variant)
