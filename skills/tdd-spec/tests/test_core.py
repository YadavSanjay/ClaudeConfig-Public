"""Unit-level checks: signatures and stubs, the classifier (on real pytest outcomes),
wrong-value variants, and spec_lint rules."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tdd
import tdd_plugin
import tddlib as tl
from conftest import write



# =========================================================================== signatures
BASE = '''\


def existing(x: float) -> float:
    return x * 2


class Curve:
    def rate(self, t: float) -> float:
        return 0.01
'''


def test_stub_and_signature() -> None:
    src = BASE + '\n\ndef new(a: int, *, b: str = "x") -> list[int]:\n    """doc"""\n    raise NotImplementedError("tdd:c1")\n'
    assert tl.stubs_for(src, "c1") == {"new": 'def new(a: int, *, b: str=\'x\') -> list[int]'}
    assert tl.stubs_for(src, "c2") == {}


def test_signature_ignores_body_and_formatting() -> None:
    a = tl.functions("def f(a:int,b=2)->int:\n    return a\n")["f"]
    b = tl.functions("def f(a: int, b = 2) -> int:\n    # comment\n    return a + b\n")["f"]
    assert tl.signature(a) == tl.signature(b)


def test_signature_sees_decorators_and_async() -> None:
    fns = tl.functions("class C:\n    @staticmethod\n    async def g(x): ...\n")
    assert tl.signature(fns["C.g"]) == "@staticmethod async def g(x)"


def test_malformed_stub() -> None:
    src = 'def f():\n    x = 1\n    raise NotImplementedError("tdd:c1")\n'
    assert tl.malformed_stubs(src) == ["f"]


def test_stub_only_accepts_added_stubs_imports_and_classes() -> None:
    head = ("from typing import Any\n" + BASE +
            '\n\ndef new() -> int:\n    raise NotImplementedError("tdd:c1")\n'
            '\n\nclass Pricer:\n    """New."""\n\n    def price(self) -> float:\n'
            '        raise NotImplementedError("tdd:c1")\n')
    assert tl.stub_only_errors(BASE, head, "m.py") == []


def test_stub_only_rejects_changes() -> None:
    changed = BASE.replace("x * 2", "x * 3")
    assert any("existing function existing changed" in e for e in tl.stub_only_errors(BASE, changed, "m.py"))
    added_impl = BASE + "\n\ndef new() -> int:\n    return 1\n"
    assert any("not a stub" in e for e in tl.stub_only_errors(BASE, added_impl, "m.py"))
    added_stmt = BASE + "\nX = 1\n"
    assert any("outside added stubs" in e for e in tl.stub_only_errors(BASE, added_stmt, "m.py"))
    assert tl.stub_only_errors(BASE, None, "m.py") == ["m.py: file deleted"]


def test_new_module_of_stubs() -> None:
    head = 'def f() -> int:\n    raise NotImplementedError("tdd:c1")\n'
    assert tl.stub_only_errors(None, head, "new.py") == []


def test_module_name() -> None:
    assert tl.module_name("src/pricing/bonds.py", ["src"]) == "pricing.bonds"
    assert tl.module_name("src/pricing/__init__.py", ["src", "."]) == "pricing"
    assert tl.module_name("app/x.py", ["."]) == "app.x"
    assert tl.module_name("other/x.py", ["src"]) is None


# =========================================================================== classify
CASES = '''\
import pytest


def test_assert():
    assert 1 == 2


def test_nie():
    raise NotImplementedError("tdd:c9")


def test_nie_other_id():
    raise NotImplementedError("tdd:other")


def test_nie_plain():
    raise NotImplementedError


def test_did_not_raise():
    with pytest.raises(ValueError):
        pass


def test_group_right():
    raise ExceptionGroup("hypothesis found 2 failures", [AssertionError("a"),
                         NotImplementedError("tdd:c9")])


def test_group_mixed():
    raise ExceptionGroup("hypothesis found 2 failures", [AssertionError("a"), KeyError("k")])


def test_nested_group():
    raise ExceptionGroup("outer", [ExceptionGroup("inner", [AssertionError("a")])])


def test_other():
    raise KeyError("k")


@pytest.mark.skip
def test_skipped():
    pass


@pytest.mark.xfail
def test_xfail():
    assert False


@pytest.fixture
def broken():
    raise RuntimeError("fixture")


def test_setup_error(broken):
    pass


def test_pass():
    assert True
'''

EXPECTED = {
    "test_assert": tl.RIGHT, "test_nie": tl.RIGHT, "test_nie_other_id": tl.WRONG,
    "test_nie_plain": tl.WRONG, "test_did_not_raise": tl.RIGHT, "test_group_right": tl.RIGHT,
    "test_group_mixed": tl.WRONG, "test_nested_group": tl.RIGHT, "test_other": tl.WRONG,
    "test_skipped": tl.WRONG, "test_xfail": tl.WRONG, "test_setup_error": tl.WRONG, "test_pass": tl.PASS,
}


def test_classifier_on_real_outcomes(project: Path) -> None:
    write(project, "tests/spec/c9/test_cases.py", CASES)
    prof = tl.load_profile(project)
    assert prof is not None
    res = tdd.run_tests(project, prof, ["tests/spec/c9"])
    assert res.report_ok, res.output
    got = {nid.rsplit("::", 1)[1]: tl.classify(res.outcome(nid), "c9") for nid in res.collected}
    assert got == EXPECTED


def test_bug_fix_mode_rejects_nie() -> None:
    rec = {"outcome": "failed", "when": "call",
           "exc": {"type": "builtins.NotImplementedError", "args": ["tdd:c9"]}}
    assert tl.classify(rec, "c9") == tl.RIGHT
    assert tl.classify(rec, "c9", allow_nie=False) == tl.WRONG


def test_missing_and_empty_group() -> None:
    assert tl.classify(None, "c9") == tl.WRONG
    empty = {"outcome": "failed", "when": "call", "exc": {"type": "builtins.ExceptionGroup", "members": []}}
    assert tl.classify(empty, "c9") == tl.WRONG


# =========================================================================== variants
@dataclass
class Leg:
    notional: float
    ccy: str


@dataclass
class Trade:
    id: int
    legs: list[Leg]
    leg: Leg
    tags: dict[str, str] = field(default_factory=dict)


def _v(hint, variant):  # type: ignore[no-untyped-def]
    return tdd_plugin.value_for(hint, variant)()


def test_numeric() -> None:
    assert _v(float, "zero") == 0.0 and math.isnan(_v(float, "nan")) and _v(float, "none") is None
    assert _v(int, "zero") == 0 and _v(int, "empty") is None


def test_str_and_containers() -> None:
    assert _v(str, "empty") == "" and _v(str, "zero") is None
    assert _v(list[int], "empty") == [] and _v(dict[str, float], "empty") == {}
    assert _v(tuple, "empty") == ()


def test_optional_and_missing() -> None:
    assert _v(Optional[float], "zero") == 0.0
    assert _v(float | None, "nan") != _v(float | None, "nan")
    assert _v(tdd_plugin._MISSING, "zero") is None and _v(None, "zero") is None


def test_dataclass_zero_instance_two_levels() -> None:
    t = _v(Trade, "zero_instance")
    assert isinstance(t, Trade) and t.id == 0 and t.legs == [] and t.tags == {}
    assert isinstance(t.leg, Leg) and t.leg.notional == 0.0 and t.leg.ccy == ""
    assert _v(Trade, "zero") is None


def test_applicable() -> None:
    assert tdd_plugin.applicable(float, "nan") and not tdd_plugin.applicable(str, "nan")
    assert tdd_plugin.applicable(Leg, "zero_instance") and tdd_plugin.applicable(Leg, "none")


# =========================================================================== spec_lint
BAD = '''\
import datetime
import requests
import pytest
from unittest import mock
from pricing import bonds
from hypothesis import given, settings


def test_no_tags():
    assert bonds.discount_factor(0.0, 1.0) == 1.0


@pytest.mark.criterion("C1")
@pytest.mark.skip
def test_skip():
    # oracle: x
    pass


@pytest.mark.criterion("C1")
def test_mocks(monkeypatch):
    # oracle: x
    mock.patch("pricing.bonds.discount_factor")
    monkeypatch.setattr(bonds, "discount_factor", lambda r, t: 1)
    open("data/marketdata/x.csv")
    datetime.date.today()


@pytest.mark.criterion("C1")
@settings(derandomize=False)
@given()
def test_settings():
    # oracle: x
    pass
'''


def _rules(tmp_path: Path, text: str) -> list[str]:
    p = tmp_path / "test_x.py"
    p.write_text(text, encoding="utf-8")
    v, _ = tdd.lint_test_file(p, "tests/spec/c1/test_x.py", {"pricing"})
    return sorted({x.rule for x in v})


def test_all_rules_fire(tmp_path: Path) -> None:
    assert _rules(tmp_path, BAD) == ["clock", "criterion", "float-eq", "hypothesis-settings", "live-data",
                                     "mock-src", "network", "oracle", "skip"]


def test_clean_file(tmp_path: Path) -> None:
    good = ('import math\nimport pytest\n\n@pytest.mark.criterion("C1")\ndef test_a():\n'
            '    # oracle: closed form\n    assert math.exp(0) == pytest.approx(1.0)\n')
    assert _rules(tmp_path, good) == []


def test_property_detection(tmp_path: Path) -> None:
    src = ('import pytest\nfrom hypothesis import given, strategies as st\n\n'
           '@pytest.mark.criterion("C1")\n@given(st.floats())\ndef test_p(x):\n    # oracle: parity\n    pass\n\n'
           '@pytest.mark.criterion("C2")\n@pytest.mark.metamorphic\ndef test_m():\n    # oracle: scaling\n    pass\n\n'
           '@pytest.mark.criterion("C2")\ndef test_point():\n    # oracle: hand calc\n'
           '    # red-exempt: none  refusal returns None\n    pass\n')
    p = tmp_path / "test_p.py"
    p.write_text(src, encoding="utf-8")
    _, tests = tdd.lint_test_file(p, "t.py", set())
    props = {t.name: t.is_property for t in tests}
    assert props == {"test_p": True, "test_m": True, "test_point": False}
    assert tests[2].red_exempt == ["none  refusal returns None"]


def test_spec_criteria() -> None:
    text = "# x\n## Requirement\n- C9 not here\n## Acceptance criteria\n- **C1**: a\n* C2 - b\n  - C1 again\n## Interface\n- C3\n"
    assert tdd.spec_criteria(text) == ["C1", "C2"]


# =========================================================================== review fixes
def test_stub_only_rejects_code_in_stub_headers() -> None:
    """A stub's header (defaults, annotations, decorators) and a new class's bases, keywords and
    decorators run at import time: no calls there, only allow-listed decorators."""
    bad = {
        "call default": 'def f(x=__import__("os").getcwd()):\n    raise NotImplementedError("tdd:a")\n',
        "decorator": '@__import__("functools").lru_cache\ndef f():\n    raise NotImplementedError("tdd:a")\n',
        "call annotation": 'def f(x: g()) -> int:\n    raise NotImplementedError("tdd:a")\n',
        "class base call": 'class K(type("B", (), {})):\n    def m(self):\n        raise NotImplementedError("tdd:a")\n',
        "class keyword call": 'class K(metaclass=g()):\n    def m(self):\n        raise NotImplementedError("tdd:a")\n',
        "class decorator": '@register\nclass K:\n    def m(self):\n        raise NotImplementedError("tdd:a")\n',
        "lambda default": 'def f(x=lambda: 1):\n    raise NotImplementedError("tdd:a")\n',
    }
    for name, head in bad.items():
        assert tl.stub_only_errors(BASE, BASE + "\n\n" + head, "m.py"), name


def test_stub_only_accepts_plain_headers() -> None:
    head = BASE + ('\n\nclass Pricer(Base, metaclass=abc.ABCMeta):\n'
                   '    @staticmethod\n    def a(x: float = -1.0, ccy: str = "USD") -> list[float]:\n'
                   '        raise NotImplementedError("tdd:a")\n\n'
                   '    @property\n    def b(self) -> Curve.Kind:\n        raise NotImplementedError("tdd:a")\n\n'
                   '    @abc.abstractmethod\n    def c(self, mode=Mode.FAST, n: int | None = None):\n'
                   '        raise NotImplementedError("tdd:a")\n')
    assert tl.stub_only_errors(BASE, head, "m.py") == []


def test_stub_only_rejects_imports_that_rebind_existing_names() -> None:
    """An added import must not replace a name the module already defines (a silent behaviour change)."""
    stub = '\n\ndef zero_price(f: float) -> float:\n    raise NotImplementedError("tdd:c1")\n'
    for imp in ("from evil import existing", "import evil as existing", "from evil import Curve",
                "from evil import *", "from evil import zero_price"):
        errs = tl.stub_only_errors(BASE, BASE + "\n" + imp + "\n" + stub, "m.py")
        assert errs, imp
    nested = BASE.replace("    def rate(self, t: float) -> float:",
                          "    from evil import rate\n\n    def rate2(self, t: float) -> float:")
    assert tl.stub_only_errors(BASE, nested, "m.py")
    assert tl.stub_only_errors(BASE, "from typing import Protocol\n" + BASE + stub, "m.py") == []
