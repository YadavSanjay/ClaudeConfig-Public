"""tdd: the tdd-spec tools and command line. Design: ../design/plan.md.

    tdd init <repo>        vendor the tools into <repo>/tools/tdd and add any missing pack files
                           (profile.toml, protected.txt, <spec_root>/conftest.py, the two workflows)
    tdd lint <id>          spec_lint on <spec_root>/<id>/ and the stubs
    tdd red <id>           red_check (both passes)
    tdd lock <id>          lint + red check, then write .claude/tdd/locks/<id>.json
    tdd run <id>           controlled run of one change, classified per test
    tdd check              integrity of the working tree against origin/main
    tdd gate               what the Stop gate would decide now (no counters change)
    tdd verify <id>        fresh-seed rerun; failures go to <dispute_dir>/<id>.md
    tdd status             locks, states and warnings
    tdd retire <id> --record TEXT   retire a lock by record (maintenance branch; history not in this repo)
    tdd path-guard --base <rev> --head <rev> [--head-ref <branch>]     CI: classify a PR
    tdd ci [--head-ref <branch>]                                       CI: spec and regression tests
    tdd manifest-check     verify tools/tdd against its MANIFEST.json

Options: --repo <path> (default: the git toplevel of the current directory).
Stdlib only, so the Stop hook can import it under `python -I -S`; pytest runs in a subprocess.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tddlib as tl  # noqa: E402

# =========================================================================== runner
# The controlled spec-test run shared by red_check, lock, the Stop gate, verify and CI (§9.3). The
# repo's own pytest setup is ignored: a reduced environment, this config, explicit --rootdir and
# --confcutdir, plugin autoload off with only the profile's plugins, and tdd_plugin for outcomes.
GATE_INI = """\
[pytest]
markers =
    criterion(id): the acceptance criterion this test covers, e.g. criterion("C1")
    metamorphic: a property or metamorphic test that does not use hypothesis
python_files = test_*.py *_test.py
xfail_strict = true
addopts =
"""

# Variables an interpreter and common libraries need on Windows and POSIX; nothing PYTHON*/PYTEST*.
_OS_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL")


@dataclass
class RunResult:
    returncode: int
    collected: list[str] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    output: str = ""
    report_ok: bool = True

    def outcome(self, nodeid: str) -> dict[str, Any] | None:
        return self.results.get(nodeid)


# The test interpreter starts isolated (-I: no current directory or PYTHONPATH on sys.path, no user
# site) and then appends, in this order, after the standard library and site-packages: the tools
# directory (tdd_plugin, tddlib), the profile's pythonpath, and the spec root (for _shared helpers).
# A project module can't shadow pytest, the standard library or the gate's plugin, because each of
# those is found first.
_BOOT = ("import os, sys; "
         "sys.path[:] = [p for p in sys.path if p not in ('', '.')] + os.environ['TDD_SYS_PATH'].split(os.pathsep); "
         "import pytest; sys.exit(pytest.main(sys.argv[1:]))")


def python_exe() -> str:
    """TDD_PYTHON is a test seam only; the hooks remove it from their environment (hooks.sanitize_env)."""
    return os.environ.get("TDD_PYTHON") or sys.executable


def clean_env(top: Path, profile: tl.Profile, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _OS_KEEP}
    prefix = Path(python_exe()).resolve().parent                    # conda env DLLs on Windows
    extra_path = [prefix, prefix / "Library" / "bin", prefix / "Scripts"] if os.name == "nt" else []
    env["PATH"] = os.pathsep.join([*map(str, extra_path), env.get("PATH", "")])
    project = [str((top / p).resolve()) for p in profile.pythonpath]
    env.update({"TDD_SYS_PATH": os.pathsep.join([str(HERE), *project, str((top / profile.spec_root).resolve())]),
                "PYTHONPATH": os.pathsep.join(project),           # child processes only; -I ignores it
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    env.update(extra or {})
    return env


def run_tests(top: Path, profile: tl.Profile, paths: list[str], *, hypothesis_profile: str | None = None,
              extra_args: list[str] | None = None, extra_env: dict[str, str] | None = None,
              collect_only: bool = False, timeout: float = 1200) -> RunResult:
    with tempfile.TemporaryDirectory(prefix="tdd-run-") as tmp:
        ini = Path(tmp) / "pytest-gate.ini"
        ini.write_text(GATE_INI, encoding="utf-8")
        report = Path(tmp) / "report.json"
        cmd = [python_exe(), "-I", "-c", _BOOT, "-c", str(ini), "--rootdir", str(top),
               "--confcutdir", str((top / profile.spec_root).resolve()), "--import-mode=importlib",
               "-p", "tdd_plugin", "-p", "no:cacheprovider", "--strict-markers", "-q", "-rN",
               "--color=no"]
        for p in profile.plugins:
            cmd += ["-p", p]
        if profile.uses_hypothesis and hypothesis_profile:
            cmd += [f"--hypothesis-profile={hypothesis_profile}"]
        if collect_only:
            cmd += ["--collect-only"]
        cmd += [*(extra_args or []), *paths]
        env = clean_env(top, profile, {"TDD_REPORT": str(report), **(extra_env or {})})
        try:
            r = subprocess.run(cmd, cwd=top, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            rc, out = r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as e:
            partial = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc, out = -1, f"timed out after {timeout}s\n{partial}"
        res = RunResult(rc, output=out[-20000:])
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            res.collected, res.results = data["collected"], data["results"]
        except (OSError, ValueError, KeyError):
            res.report_ok = False
        return res


def spec_id_of(nodeid: str, profile: tl.Profile) -> str | None:
    """The change id a spec test belongs to: <spec_root>/<id>/..."""
    path = nodeid.split("::", 1)[0]
    root = profile.spec_root.strip("/") + "/"
    if not path.startswith(root):
        return None
    rest = path[len(root):].split("/", 1)
    return rest[0] if len(rest) == 2 else None


# =========================================================================== spec_lint
CRITERIA_HEADING = re.compile(r"^#{2,3}\s*acceptance criteria\b", re.IGNORECASE)
CRITERION_ITEM = re.compile(r"^\s*[-*]\s*\**\s*(C\d+)\b")
DATA_PATH = re.compile(r"(^|[\\/])data[\\/]")
NETWORK_MODULES = {"requests", "httpx", "urllib.request", "http.client", "socket", "aiohttp",
                   "boto3", "botocore", "openai", "anthropic", "websockets", "urllib3"}
SKIP_NAMES = {"skip", "skipif", "xfail", "importorskip"}
PATCH_CALLS = {"patch", "patch.object", "mock.patch", "mock.patch.object", "mocker.patch",
               "mocker.patch.object", "monkeypatch.setattr", "monkeypatch.delattr", "unittest.mock.patch"}
CLOCK_CALLS = {"date.today", "datetime.now", "datetime.utcnow", "datetime.today", "time.time",
               "time.time_ns", "datetime.date.today", "datetime.datetime.now",
               "datetime.datetime.utcnow", "datetime.datetime.today"}


@dataclass
class Violation:
    file: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: [{self.rule}] {self.message}"


@dataclass
class TestInfo:
    file: str
    name: str                 # function name, or Class::function
    line: int
    criteria: list[str] = field(default_factory=list)
    is_property: bool = False
    has_oracle: bool = False
    red_exempt: list[str] = field(default_factory=list)


@dataclass
class LintResult:
    violations: list[Violation] = field(default_factory=list)
    tests: list[TestInfo] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def red_exempt(self) -> list[str]:
        return [f"{t.file}::{t.name}: {e}" for t in self.tests for e in t.red_exempt]


# =========================================================================== spec file
def spec_criteria(text: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = bool(CRITERIA_HEADING.match(line))
            continue
        if inside and (m := CRITERION_ITEM.match(line)):
            out.append(m.group(1))
    return list(dict.fromkeys(out))


# =========================================================================== helpers
def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return ""


def _comments(source: str) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _is_float_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, float)


def _mark_args(deco: ast.expr, mark: str) -> list[str] | None:
    """String args of `pytest.mark.<mark>(...)` / `mark.<mark>(...)`, None if not that mark."""
    name = dotted(deco)
    if not (name == mark or name.endswith(f"mark.{mark}")):
        return None
    if isinstance(deco, ast.Call):
        return [a.value for a in deco.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return []


def src_packages(top: Path, profile: tl.Profile) -> set[str]:
    names: set[str] = set()
    for root in profile.src_roots:
        r = top / root
        if not r.is_dir():
            continue
        for p in r.iterdir():
            if p.is_dir() and any(p.glob("*.py")):
                names.add(p.name)
            elif p.suffix == ".py" and p.stem != "__init__":
                names.add(p.stem)
        if any(r.glob("*.py")) and (r / "__init__.py").exists():
            names.add(r.name)
    return names


# =========================================================================== test files
def lint_test_file(path: Path, rel: str, src_pkgs: set[str]) -> tuple[list[Violation], list[TestInfo]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        return [Violation(rel, e.lineno or 0, "syntax", str(e))], []
    comments = _comments(source)
    v: list[Violation] = []
    src_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in NETWORK_MODULES or a.name.split(".")[0] in {"requests", "httpx", "aiohttp",
                                                                       "boto3", "openai", "anthropic"}:
                    v.append(Violation(rel, node.lineno, "network", f"imports {a.name}"))
                if a.name.split(".")[0] in src_pkgs:
                    src_aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in NETWORK_MODULES or node.module.split(".")[0] in {"requests", "httpx",
                                                                               "aiohttp", "boto3",
                                                                               "openai", "anthropic"}:
                v.append(Violation(rel, node.lineno, "network", f"imports from {node.module}"))
            if node.module.split(".")[0] in src_pkgs:
                for a in node.names:
                    src_aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Compare):
            if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops) and \
                    any(_is_float_literal(x) for x in [node.left, *node.comparators]) and \
                    not any("approx" in dotted(x) or "isclose" in dotted(x)
                            for x in [node.left, *node.comparators]):
                v.append(Violation(rel, node.lineno, "float-eq",
                                   "float compared with ==; use pytest.approx/isclose with a tolerance"))
        elif isinstance(node, ast.Attribute) and node.attr in SKIP_NAMES:
            if dotted(node.value).split(".")[0] in {"pytest", "mark"}:
                v.append(Violation(rel, node.lineno, "skip", f"uses {dotted(node)}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and DATA_PATH.search(node.value):
            v.append(Violation(rel, node.lineno, "live-data", f"refers to data path {node.value!r}"))
        elif isinstance(node, ast.Call):
            name = dotted(node.func)
            if name in CLOCK_CALLS:
                v.append(Violation(rel, node.lineno, "clock", f"calls {name}(); use the fixed valuation date"))
            if name in PATCH_CALLS and node.args:
                target = node.args[0]
                tgt = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) \
                    else src_aliases.get(dotted(target).split(".")[0], "") if dotted(target) else ""
                if tgt and tgt.split(".")[0] in src_pkgs:
                    v.append(Violation(rel, node.lineno, "mock-src", f"{name} targets project code {tgt!r}"))
            if name.split(".")[-1] == "settings":
                for kw in node.keywords:
                    if kw.arg in ("derandomize", "database"):
                        v.append(Violation(rel, node.lineno, "hypothesis-settings",
                                           f"per-test settings override {kw.arg}"))

    tests = list(_collect_tests(tree, rel, comments))
    for t in tests:
        if not t.has_oracle:
            v.append(Violation(rel, t.line, "oracle", f"{t.name} has no '# oracle: <source>' comment"))
        if not t.criteria:
            v.append(Violation(rel, t.line, "criterion", f"{t.name} has no @pytest.mark.criterion(...)"))
    return v, tests


def _collect_tests(tree: ast.Module, rel: str, comments: dict[int, str]):  # type: ignore[no-untyped-def]
    def info(fn: tl.Func, name: str, class_decos: list[ast.expr]) -> TestInfo:
        decos = [*class_decos, *fn.decorator_list]
        crit: list[str] = []
        prop = False
        for d in decos:
            if (args := _mark_args(d, "criterion")) is not None:
                crit.extend(args)
            if _mark_args(d, "metamorphic") is not None or dotted(d).split(".")[-1] == "given":
                prop = True
        first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
        span = range(max(1, first - 3), (fn.end_lineno or fn.lineno) + 1)
        texts = [comments[i] for i in span if i in comments]
        exempt = [c.split("red-exempt:", 1)[1].strip() for c in texts if "red-exempt:" in c]
        return TestInfo(rel, name, fn.lineno, crit, prop, any("oracle:" in c for c in texts), exempt)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield info(node, node.name, [])
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                    yield info(fn, f"{node.name}::{fn.name}", node.decorator_list)


# =========================================================================== src stubs
def changed_src_files(top: Path, profile: tl.Profile, base: str = tl.MAIN) -> list[str]:
    files: set[str] = set()
    if tl.has_ref(top, base):
        mb = tl.git(top, "merge-base", base, "HEAD", check=False).strip() or base
        files |= set(tl.nul_split(tl.git(top, "diff", "--name-only", "-z", mb, check=False)))
    files |= set(tl.nul_split(tl.git(top, "ls-files", "-z", "--others", "--exclude-standard", check=False)))
    return sorted(f for f in files if f.endswith(".py") and any(tl.under(f, r) for r in profile.src_roots))


def lint_stubs(top: Path, profile: tl.Profile, cid: str, base: str = tl.MAIN) -> list[Violation]:
    v: list[Violation] = []
    mb = tl.git(top, "merge-base", base, "HEAD", check=False).strip() if tl.has_ref(top, base) else ""
    for rel in changed_src_files(top, profile, base):
        p = top / rel
        head = p.read_text(encoding="utf-8") if p.exists() else None
        base_src = tl.show(top, mb, rel) if mb else None
        for err in tl.stub_only_errors(base_src, head, rel):
            v.append(Violation(rel, 0, "stub-only", err))
        if head is not None:
            for q in tl.malformed_stubs(head):
                v.append(Violation(rel, 0, "stub-body", f"{q}: body must be only "
                                   f"raise NotImplementedError(\"tdd:{cid}\")"))
    return v


# =========================================================================== entry
def lint(top: Path, profile: tl.Profile, cid: str, *, check_stubs: bool = True) -> LintResult:
    res = LintResult()
    spec = top / profile.specs_dir / f"{cid}.md"
    if not spec.exists():
        res.violations.append(Violation(spec.relative_to(top).as_posix(), 0, "spec", "spec file missing"))
    else:
        res.criteria = spec_criteria(spec.read_text(encoding="utf-8"))
        if not res.criteria:
            res.violations.append(Violation(spec.relative_to(top).as_posix(), 0, "spec",
                                            "no criteria (C1, C2, ...) under '## Acceptance criteria'"))
    test_dir = top / profile.spec_root / cid
    if not test_dir.is_dir():
        res.violations.append(Violation(f"{profile.spec_root}/{cid}", 0, "tests", "no spec tests"))
        return res
    pkgs = src_packages(top, profile)
    for p in sorted(test_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(top).as_posix()
        v, tests = lint_test_file(p, rel, pkgs)
        res.violations += v
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            res.tests += tests
    known = set(res.criteria)
    for t in res.tests:
        for c in t.criteria:
            if c not in known:
                res.violations.append(Violation(t.file, t.line, "criterion", f"{t.name}: unknown criterion {c}"))
    for c in res.criteria:
        point = [t for t in res.tests if c in t.criteria and not t.is_property]
        prop = [t for t in res.tests if c in t.criteria and t.is_property]
        if not point:
            res.violations.append(Violation(f"{profile.specs_dir}/{cid}.md", 0, "coverage",
                                            f"{c} has no point-oracle test"))
        if not prop:
            res.violations.append(Violation(f"{profile.specs_dir}/{cid}.md", 0, "coverage",
                                            f"{c} has no property or metamorphic test"))
    if check_stubs:
        res.violations += lint_stubs(top, profile, cid)
    return res


# =========================================================================== red_check
@dataclass
class RedResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def interface_items(top: Path, profile: tl.Profile, cid: str) -> list[dict[str, Any]]:
    """The stubs for `cid` in the working tree: [{file, module, qualname, signature}]."""
    marker = f"tdd:{cid}"
    items = []
    for root in profile.src_roots:
        for p in sorted((top / root).rglob("*.py")) if (top / root).is_dir() else []:
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8")
            if marker not in text:
                continue
            rel = p.relative_to(top).as_posix()
            mod = tl.module_name(rel, profile.pythonpath)
            for q, sig in tl.stubs_for(text, cid).items():
                items.append({"file": rel, "module": mod, "qualname": q, "signature": sig})
    return items


def _test_key(nodeid: str) -> tuple[str, str]:
    path, _, rest = nodeid.partition("::")
    return path, rest.split("[", 1)[0]


def _exemptions(lint: LintResult) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for t in lint.tests:
        out[(t.file, t.name)] = {e.split()[0].lower() for e in t.red_exempt if e.split()}
    return out


def red_check(top: Path, profile: tl.Profile, cid: str, *, lint_res: LintResult | None = None) -> RedResult:
    res = RedResult(items=interface_items(top, profile, cid))
    test_dir = f"{profile.spec_root}/{cid}"
    allow_nie = bool(res.items)
    if not allow_nie:
        res.notes.append("no stubs found: bug-fix mode, only assertion failures count")
    base = run_tests(top, profile, [test_dir], hypothesis_profile=profile.gate_profile)
    if not base.report_ok or not base.collected:
        res.problems.append("pass 1: no tests collected or the run failed\n" + base.output[-3000:])
        return res
    for nid in base.collected:
        rec = base.outcome(nid)
        verdict = tl.classify(rec, cid, allow_nie=allow_nie)
        if verdict == tl.PASS:
            res.problems.append(f"pass 1: {nid} passes against the current code")
        elif verdict == tl.WRONG:
            res.problems.append(f"pass 1: {nid} fails for the wrong reason: {tl.describe(rec)}")
    if not res.items or res.problems:
        return res

    exempt = _exemptions(lint_res or lint(top, profile, cid, check_stubs=False))
    with tempfile.TemporaryDirectory(prefix="tdd-red-") as tmp:
        iface = Path(tmp) / "interface.json"
        iface.write_text(json.dumps([{"module": i["module"], "qualname": i["qualname"]}
                                     for i in res.items]), encoding="utf-8")
        for variant in tl.VARIANTS:
            run = run_tests(top, profile, [test_dir], hypothesis_profile=profile.gate_profile,
                            extra_env={"TDD_VARIANT": variant, "TDD_INTERFACE": str(iface)})
            if not run.report_ok:
                res.problems.append(f"pass 2 ({variant}): the run failed\n" + run.output[-3000:])
                continue
            for nid in run.collected:
                if tl.classify(run.outcome(nid), cid) != tl.PASS:
                    continue
                if variant in exempt.get(_test_key(nid), set()):
                    res.notes.append(f"pass 2 ({variant}): {nid} passes, exempted by red-exempt")
                else:
                    res.problems.append(f"pass 2 ({variant}): {nid} passes against a stub returning a "
                                        f"wrong value; the test does not pin the result")
    return res


# =========================================================================== lock
SCHEMA = "tdd-lock/1"


class LockRefused(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def absence_records(top: Path, profile: tl.Profile, cid: str) -> list[str]:
    parts = Path(profile.spec_root.strip("/"), cid).parts
    candidates = ["/".join([*parts[:i], "conftest.py"]) for i in range(len(parts) + 1)]
    for d in [".", *profile.pythonpath]:
        base = "" if d.strip("/") in ("", ".") else d.strip("/") + "/"
        candidates += [f"{base}sitecustomize.py", f"{base}usercustomize.py"]
    # <spec_root>/conftest.py is a maintenance path, checked against origin/main instead.
    maint = profile.spec_maintenance_globs()
    return sorted({c for c in candidates if not (top / c).exists() and not tl.glob_match(c, maint)})


def criteria_map(lint: LintResult, nodes: list[str]) -> dict[str, list[str]]:
    by_test = {(t.file, t.name): t for t in lint.tests}
    out: dict[str, list[str]] = {c: [] for c in lint.criteria}
    for nid in nodes:
        t = by_test.get(_test_key(nid))
        for c in t.criteria if t else []:
            out.setdefault(c, []).append(nid)
    return out


def build_lock(top: Path, profile: tl.Profile, cid: str) -> dict[str, Any]:
    lint_res = lint(top, profile, cid)
    if not lint_res.ok:
        raise LockRefused([str(v) for v in lint_res.violations])
    red = red_check(top, profile, cid, lint_res=lint_res)
    if not red.ok:
        raise LockRefused(red.problems)
    coll = run_tests(top, profile, [f"{profile.spec_root}/{cid}"], collect_only=True,
                      hypothesis_profile=profile.gate_profile)
    if not coll.collected:
        raise LockRefused(["no tests collected"])
    spec_rel = f"{profile.specs_dir}/{cid}.md"
    return {
        "schema": SCHEMA,
        "id": cid,
        "status": "active",
        "tool_version": tl.VERSION,
        "created": dt.date.today().isoformat(),
        "spec": {"path": spec_rel, "sha256": tl.sha256_file(top / spec_rel)},
        "files": tl.tree_files(top, f"{profile.spec_root}/{cid}"),
        "interface": [{k: i[k] for k in ("file", "module", "qualname", "signature")} for i in red.items],
        "absent": absence_records(top, profile, cid),
        "nodes": sorted(coll.collected),
        "criteria": criteria_map(lint_res, coll.collected),
        "red_exempt": lint_res.red_exempt(),
    }


def write_lock(top: Path, lock: dict[str, Any]) -> Path:
    p = top / tl.lock_path(lock["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def check_signatures(top: Path, lock: dict[str, Any]) -> list[str]:
    problems = []
    for item in lock.get("interface", []):
        p = top / item["file"]
        if not p.exists():
            problems.append(f"{item['file']}: missing; locked {item['qualname']} gone")
            continue
        try:
            fns = tl.functions(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            problems.append(f"{item['file']}: syntax error: {e}")
            continue
        fn = fns.get(item["qualname"])
        if fn is None:
            problems.append(f"{item['file']}: locked {item['qualname']} removed")
        elif tl.signature(fn) != item["signature"]:
            problems.append(f"{item['file']}: signature of {item['qualname']} changed: "
                            f"{tl.signature(fn)!r} != locked {item['signature']!r}")
    return problems


def check_files(top: Path, profile: tl.Profile, lock: dict[str, Any]) -> list[str]:
    now = tl.tree_files(top, f"{profile.spec_root}/{lock['id']}")
    locked = lock.get("files", {})
    problems = [f"{f}: changed" for f in sorted(set(now) & set(locked)) if now[f] != locked[f]]
    problems += [f"{f}: added" for f in sorted(set(now) - set(locked))]
    problems += [f"{f}: removed" for f in sorted(set(locked) - set(now))]
    return problems


def check_absent(top: Path, lock: dict[str, Any]) -> list[str]:
    return [f"{p}: must not exist (absence record)" for p in lock.get("absent", []) if (top / p).exists()]


def check_lock(top: Path, profile: tl.Profile, lock: dict[str, Any], *, files: bool = True) -> list[str]:
    """files=False (spec branches and spec PRs): spec tests may change, including a conftest.py
    added under <spec_root>/<id>/; signatures and the other absence records stay checked."""
    absent = check_absent(top, lock)
    if not files:
        absent = [a for a in absent if not tl.under(a.split(":", 1)[0], profile.spec_root)]
    out = check_signatures(top, lock) + absent
    return (check_files(top, profile, lock) + out) if files else out


# Project code runs inside the test process, so it could tamper with the gate's report in ways no
# isolation prevents. It can be detected: src/ must not refer to the gate or import test machinery.
GATE_NAMES = re.compile(r"\b(tdd_plugin|tddlib|TDD_REPORT|TDD_VARIANT|TDD_INTERFACE|TDD_SYS_PATH)\b")
TEST_MODULES = {"pytest", "_pytest", "hypothesis", "pluggy"}


def gate_reference_problems(top: Path, profile: tl.Profile) -> list[str]:
    out: list[str] = []
    for root in profile.src_roots:
        base = top / root
        for p in sorted(base.rglob("*.py")) if base.is_dir() else []:
            if "__pycache__" in p.parts:
                continue
            rel, text = p.relative_to(top).as_posix(), p.read_text(encoding="utf-8", errors="replace")
            if m := GATE_NAMES.search(text):
                out.append(f"{rel}: refers to the tdd gate ({m.group(1)})")
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else \
                    [node.args[0].value] if isinstance(node, ast.Call) and node.args and \
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and \
                    dotted(node.func) in ("__import__", "importlib.import_module", "import_module") else []
                hit = next((n for n in names if n.split(".")[0] in TEST_MODULES), None)
                if hit:
                    out.append(f"{rel}: project code imports test machinery ({hit})")
                    break
    return out


# =========================================================================== gate
TAMPER_LIMIT = 3
MAX_LISTED = 12


@dataclass
class Integrity:
    problems: list[str] = field(default_factory=list)
    rebase: list[str] = field(default_factory=list)        # own lock is newer on main
    warnings: list[str] = field(default_factory=list)
    active: dict[str, dict[str, Any]] = field(default_factory=dict)   # applicable active locks


def _compare_with_main(top: Path, patterns: list[str]) -> list[str]:
    main_files = {f for f in tl.ls_tree(top, tl.MAIN) if tl.glob_match(f, patterns)}
    work_files = set(tl.worktree_glob(top, patterns, include_ignored=True))
    out = [f"{f}: removed (exists on origin/main)" for f in sorted(main_files - work_files)]
    out += [f"{f}: added (not on origin/main)" for f in sorted(work_files - main_files)]
    for f in sorted(main_files & work_files):
        if tl.sha256_bytes(tl.show_bytes(top, tl.MAIN, f) or b"") != tl.sha256_file(top / f):
            out.append(f"{f}: differs from origin/main")
    return out


def unrecognised_retirement(top: Path, profile: tl.Profile, cid: str, lock: dict[str, Any]) -> bool:
    if not lock.get("interface"):
        return False
    roots = [r for r in profile.src_roots]
    hits = tl.git(top, "grep", "-l", "-F", f"tdd:{cid}", tl.MAIN, "--", *roots, check=False)
    return not hits.strip()


def integrity(top: Path, profile: tl.Profile, branch: tl.Branch) -> Integrity:
    res = Integrity()
    for cid in tl.lock_ids(top):
        lock = tl.read_lock(top, cid)
        if not lock or lock.get("status") != "active" or tl.is_retired(top, cid):
            continue
        if unrecognised_retirement(top, profile, cid, lock):
            res.warnings.append(f"lock {cid}: stubs are gone from origin/main but no merge from "
                                f"impl/{cid} was found; check the merge method")
        relation, commit = tl.applicable_lock(top, cid)
        if relation == "absent":
            continue
        if relation == "stale":
            if branch.kind == "impl" and branch.id == cid:
                res.rebase.append(cid)
                continue
            lock = json.loads(tl.show(top, commit or "", tl.lock_path(cid)) or "{}") or lock
        res.active[cid] = lock
        # On spec/<id>, only <id>'s own tests may change (a re-lock); other active locks stay checked.
        own = branch.kind == "spec" and branch.id == cid
        res.problems += [f"[{cid}] {p}" for p in check_lock(top, profile, lock, files=not own)]
    res.problems += _compare_with_main(top, profile.spec_maintenance_globs())
    res.problems += gate_reference_problems(top, profile)
    if branch.kind != "spec":
        protected = tl.load_protected(top)
        if protected:
            res.problems += _compare_with_main(top, protected)
    return res


# =========================================================================== stop
def _block(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def _allow(message: str = "") -> dict[str, Any] | None:
    return {"systemMessage": message} if message else None


def _listed(lines: list[str]) -> str:
    extra = f"\n  ... and {len(lines) - MAX_LISTED} more" if len(lines) > MAX_LISTED else ""
    return "\n".join(f"  - {ln}" for ln in lines[:MAX_LISTED]) + extra


def _budget_block(top: Path, project: str, profile: tl.Profile, key: str, reason: str,
                  branch: tl.Branch) -> dict[str, Any] | None:
    st = tl.load_state(top, key)
    st["blocks"] = int(st.get("blocks", 0)) + 1
    if st["blocks"] > profile.budget:
        st["status"] = "budget-exhausted"
        tl.save_state(top, key, st)
        tl.log_event(project, "budget-exhausted", branch=branch.name, change=branch.id, blocks=st["blocks"] - 1)
        return _allow(f"tdd gate: iteration budget ({profile.budget}) used up for {key}; stopping is "
                      f"allowed. Report what is still failing and stop.\n{reason}")
    tl.save_state(top, key, st)
    tl.log_event(project, "gate-block", branch=branch.name, change=branch.id, blocks=st["blocks"])
    return _block(f"{reason}\n\n(tdd gate block {st['blocks']} of {profile.budget})")


def run_change(top: Path, profile: tl.Profile, cid: str, lock: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Controlled run of one change. Returns (failing: not passed, wrong: wrong-reason)."""
    res = run_tests(top, profile, [f"{profile.spec_root}/{cid}"], hypothesis_profile=profile.gate_profile)
    if not res.report_ok:
        msg = "the test run failed: " + res.output[-1500:]
        return [msg], [msg]
    return judge(res, cid, lock)


def judge(res: RunResult, cid: str, lock: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(failing, wrong): failing = every locked test not passing; wrong = wrong-reason failures.
    A collected set that differs from the lock's node list counts in both."""
    nodes, collected = set(lock.get("nodes", [])), set(res.collected)
    mismatch = [f"{n}: locked test not collected" for n in sorted(nodes - collected)]
    mismatch += [f"{n}: test not in the lock" for n in sorted(collected - nodes)]
    failing, wrong = list(mismatch), list(mismatch)
    for nid in sorted(nodes & collected):
        rec = res.outcome(nid)
        verdict = tl.classify(rec, cid)
        if verdict != tl.PASS:
            failing.append(f"{nid}: {tl.describe(rec)}")
        if verdict == tl.WRONG:
            wrong.append(f"{nid}: {tl.describe(rec)}")
    return failing, wrong


def stop(top: Path, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The Stop hook decision for the repo at `top`: None/systemMessage to allow, block dict to block."""
    project = tl.project_name(top)
    branch = tl.parse_branch(tl.current_branch(top))
    fetched, note = tl.fetch_main(top)
    profile = tl.load_profile(top)
    if profile is None:
        return _allow(f"tdd gate: no {tl.PROFILE} on origin/main yet; nothing is gated. {note}".strip())

    integ = integrity(top, profile, branch)
    notes = [note] if note else []
    notes += integ.warnings
    for w in integ.warnings:
        tl.log_event(project, "unrecognised-retirement", branch=branch.name, detail=w)

    # --- tampering, on any branch
    tamper = tl.load_state(top, "_tamper")
    if integ.problems:
        tamper["streak"] = int(tamper.get("streak", 0)) + 1
        tl.log_event(project, "tamper", branch=branch.name, problems=integ.problems[:20],
                     streak=tamper["streak"])
        if tamper["streak"] > TAMPER_LIMIT:
            tamper["streak"] = 0
            tl.save_state(top, "_tamper", tamper)
            tl.log_event(project, "tamper-give-way", branch=branch.name, problems=integ.problems[:20])
            return _allow("tdd gate: protected files still differ from origin/main after "
                          f"{TAMPER_LIMIT} blocks; stopping is allowed and logged. CI will flag this.")
        tl.save_state(top, "_tamper", tamper)
        return _block("tdd gate: protected files differ from origin/main or from their lock:\n"
                      f"{_listed(integ.problems)}\n\nRestore each one with\n"
                      "  git restore --source=origin/main -- <path>\n"
                      "Never change spec tests, locks or maintenance paths on this branch.")
    if tamper.get("streak"):
        tamper["streak"] = 0
        tl.save_state(top, "_tamper", tamper)

    if branch.kind == "other":
        return _allow("\n".join(notes))

    cid = branch.id or ""
    key = f"{branch.kind}-{cid}"                          # budget and status per role
    state = tl.load_state(top, key)
    if state.get("status") == "budget-exhausted":
        return _allow(f"tdd gate: budget already exhausted for {cid}.")

    # --- spec/<id>: lint and red check
    if branch.kind == "spec":
        lint_res = lint(top, profile, cid)
        problems = [str(v) for v in lint_res.violations]
        if not problems:
            red = red_check(top, profile, cid, lint_res=lint_res)
            problems = red.problems
        if problems:
            tl.log_event(project, "red-check-rejection", branch=branch.name, change=cid, problems=problems[:20])
            return _budget_block(top, project, profile, key,
                                 f"tdd gate (spec/{cid}): the spec tests are not ready to lock:\n"
                                 f"{_listed(problems)}", branch)
        lock_file = top / tl.lock_path(cid)
        if lock_file.exists():
            stale = check_lock(top, profile, json.loads(lock_file.read_text(encoding="utf-8")))
            if stale:
                return _budget_block(top, project, profile, key,
                                     f"tdd gate (spec/{cid}): the tests changed after locking; run "
                                     f"`tdd lock {cid}` again:\n{_listed(stale)}", branch)
        return _allow("\n".join([*notes, f"tdd gate: spec/{cid} passes spec_lint and red_check; "
                                          f"run `tdd lock {cid}` and open the spec PR."]))

    # --- impl/<id>
    if cid in integ.rebase:
        tl.log_event(project, "rebase-required", branch=branch.name, change=cid)
        return _budget_block(top, project, profile, key,
                             f"tdd gate: the lock for {cid} changed on main; rebase impl/{cid} onto "
                             "origin/main before continuing.", branch)
    lock = integ.active.get(cid)
    if lock is None:
        age = "" if fetched else f" ({tl.ref_age(top)})"
        return _allow("\n".join([*notes, f"tdd gate: no active lock for {cid} on origin/main{age}; "
                                          "nothing to gate. Is the spec PR merged and fetched?"]))
    dispute = top / profile.dispute_dir / f"{cid}.md"
    if dispute.exists():
        tl.log_event(project, "dispute", branch=branch.name, change=cid)
        state["status"] = "disputed"
        tl.save_state(top, key, state)
        return _allow(f"tdd gate: dispute open for {cid}; waiting for your decision.")

    failing, wrong = run_change(top, profile, cid, lock)
    blocking = wrong if branch.part is not None else failing
    if blocking:
        what = "fail for the wrong reason" if branch.part is not None else "are not passing yet"
        return _budget_block(top, project, profile, key,
                             f"tdd gate ({branch.name}): spec tests for {cid} {what}:\n{_listed(blocking)}\n\n"
                             f"Fix src/, not the tests. If you believe a test or oracle is wrong, write "
                             f"{profile.dispute_dir}/{cid}.md with your evidence and stop.", branch)
    state["status"] = "green" if branch.part is None else "implementing"
    tl.save_state(top, key, state)
    tl.log_event(project, "green" if branch.part is None else "part-ok", branch=branch.name, change=cid)
    return _allow("\n".join(notes))


# =========================================================================== verify
@dataclass
class VerifyResult:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dispute: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def verify(top: Path, profile: tl.Profile, cid: str) -> VerifyResult:
    res = VerifyResult()
    if not profile.uses_hypothesis:
        res.notes.append("hypothesis is not enabled in profile.toml; rerunning without fresh seeds")
    run = run_tests(top, profile, [f"{profile.spec_root}/{cid}"], hypothesis_profile=profile.verify_profile)
    if not run.report_ok:
        res.failures.append("the run failed:\n" + run.output[-3000:])
        return res
    detail = []
    for nid in run.collected:
        rec = run.outcome(nid)
        if tl.classify(rec, cid) != tl.PASS:
            res.failures.append(f"{nid}: {tl.describe(rec)}")
            detail.append(f"### {nid}\n\n```\n{(rec or {}).get('longrepr', '')[-4000:]}\n```\n")
    if res.failures:
        p = top / profile.dispute_dir / f"{cid}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Verify failure for {cid}\n\nFound by `tdd verify` on "
                     f"{dt.date.today().isoformat()} with the `{profile.verify_profile}` profile. Each "
                     "falsifying example below goes into the locked test as `@example(...)` through a "
                     "re-lock spec PR.\n\n" + "\n".join(detail), encoding="utf-8")
        res.dispute = p
    return res


# =========================================================================== ci
@dataclass
class CIResult:
    errors: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def tree_locks(top: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted((top / tl.LOCKS).glob("*.json")) if (top / tl.LOCKS).is_dir() else []:
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    return out


def run_ci(top: Path, head_ref: str = "") -> CIResult:
    res = CIResult()
    profile = tl.load_profile(top)
    if profile is None:
        res.summary.append(f"no {tl.PROFILE} on origin/main: nothing to check yet")
        return res
    locks = tree_locks(top)
    branch = tl.parse_branch(head_ref)
    # The same lock checks as the local gate, against the locks on main: absence records, locked
    # signatures, and (except for spec PRs) locked test files. The path guard covers the rest.
    for cid in tl.lock_ids(top):
        main_lock = tl.read_lock(top, cid)
        if main_lock and main_lock.get("status") == "active" and not tl.is_retired(top, cid):
            # A PR that re-locks <cid> (its lock differs from main's) may change <cid>'s tests; the
            # path guard requires that. Every other active lock's tests are hash-checked.
            relocked = locks.get(cid) not in (None, main_lock)
            res.errors += [f"[{cid}] {p}" for p in check_lock(top, profile, main_lock, files=not relocked)]
    # A lock this PR adds or changes must match the tests, stubs and absence records the PR carries:
    # otherwise the merged lock wouldn't pin what was reviewed.
    for cid, tree_lock in locks.items():
        if tree_lock.get("status") == "active" and tree_lock != tl.read_lock(top, cid):
            res.errors += [f"[{cid}] {p} (the PR's lock is out of date: run `tdd lock {cid}` on spec/{cid})"
                           for p in check_lock(top, profile, tree_lock)]
    res.errors += gate_reference_problems(top, profile)
    spec_dirs = sorted(p.name for p in (top / profile.spec_root).iterdir()
                       if p.is_dir() and not p.name.startswith(("_", "."))) \
        if (top / profile.spec_root).is_dir() else []
    if not spec_dirs:
        res.summary.append("no spec tests")
        return res
    run = run_tests(top, profile, [profile.spec_root], hypothesis_profile=profile.gate_profile)
    if not run.report_ok:
        res.errors.append("the spec test run failed:\n" + run.output[-4000:])
        return res
    by_id: dict[str, list[str]] = {}
    for nid in run.collected:
        by_id.setdefault(spec_id_of(nid, profile) or "?", []).append(nid)

    for cid in sorted(set(spec_dirs) | set(by_id)):
        lock = locks.get(cid)
        nodes = by_id.get(cid, [])
        if lock is None:
            res.errors.append(f"[{cid}] spec tests without a lock")
            continue
        if lock.get("status") == "abandoned":
            res.errors.append(f"[{cid}] lock is abandoned but its spec tests still exist")
            continue
        if tl.is_retired(top, cid):
            bad = [f"{n}: {tl.describe(run.outcome(n))}" for n in nodes
                   if tl.classify(run.outcome(n), cid) != tl.PASS]
            res.errors += [f"[{cid}] graduated test failing: {b}" for b in bad]
            res.summary.append(f"{cid}: graduated, {len(nodes) - len(bad)}/{len(nodes)} pass")
            continue
        sub = RunResult(run.returncode, collected=nodes, results=run.results)
        failing, wrong = judge(sub, cid, lock)
        must_pass = branch.kind == "impl" and branch.id == cid and branch.part is None
        bad = failing if must_pass else wrong
        res.errors += [f"[{cid}] {b}" for b in bad]
        locked = lock.get("nodes", [])
        passing = sum(1 for n in locked if tl.classify(run.outcome(n), cid) == tl.PASS)
        res.summary.append(f"{cid}: active{' (this PR must turn it green)' if must_pass else ''}, "
                           f"{passing}/{len(locked)} pass, {len(wrong)} wrong-reason")
    return res


# =========================================================================== path_guard
SPEC, MAINT, SRC, IMPL = "spec", "maintenance", "src", "implementation"


@dataclass
class GuardResult:
    kind: str = ""
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)        # path -> class

    @property
    def ok(self) -> bool:
        return not self.errors


def classify_path(path: str, profile: tl.Profile, protected: list[str]) -> str:
    # Case-insensitive: a case variant (`.CLAUDE/Settings.json`) is the protected file once the
    # repo is checked out on Windows, so it gets the protected class.
    ic = True
    if tl.glob_match(path, profile.maintenance_globs(), ic):
        return MAINT
    if (tl.under(path, profile.specs_dir, ic) and path.lower().endswith(".md")) or tl.under(path, profile.spec_root, ic) \
            or tl.under(path, tl.LOCKS, ic) or (protected and tl.glob_match(path, protected, ic)):
        return SPEC
    if path.endswith(".py") and any(tl.under(path, r) for r in profile.src_roots):
        return SRC
    return IMPL



def changed_files(top: Path, base: str, head: str) -> list[str]:
    return tl.nul_split(tl.git(top, "diff", "--name-only", "-z", "--no-renames", base, head))


def _locked_signature_errors(top: Path, trusted: str, head: str, changed: set[str]) -> list[str]:
    errors = []
    for cid in tl.lock_ids(top, trusted):
        lock = tl.read_lock(top, cid, trusted)
        if not lock or lock.get("status") != "active" or tl.is_retired(top, cid, trusted):
            continue
        for item in lock.get("interface", []):
            if item["file"] not in changed:
                continue
            src = tl.show(top, head, item["file"])
            fn = tl.functions(src).get(item["qualname"]) if src is not None else None
            if fn is None:
                errors.append(f"[{cid}] locked {item['file']}::{item['qualname']} removed")
            elif tl.signature(fn) != item["signature"]:
                errors.append(f"[{cid}] locked signature changed: {item['file']}::{item['qualname']}")
    return errors


def _lock_status_changes_are_maintenance(top: Path, head: str, trusted: str, res: GuardResult) -> None:
    """Reclassify a lock file whose status leaves "active" (or differs from main's non-active status).

    A lock that is not active releases its tests and signatures without an impl/<id> merge (retired by
    record, abandoned, ...). Only a human maintenance PR may do that, so the file counts as maintenance:
    the PR must then change nothing else.
    """
    for f, c in list(res.files.items()):
        if c != SPEC or not tl.under(f, tl.LOCKS):
            continue
        text = tl.show(top, head, f)
        if text is None:
            continue
        try:
            status = json.loads(text).get("status")
        except (ValueError, AttributeError):
            continue                                    # invalid JSON: reported as a spec-PR error
        before = tl.read_lock(top, Path(f).stem, trusted)
        if status != "active" and (before is None or before.get("status") != status):
            res.files[f] = MAINT


def path_guard(top: Path, base: str, head: str, head_ref: str = "", trusted: str = tl.MAIN) -> GuardResult:
    """Classify base..head. The profile, protected list and locks come from `trusted` (main), never
    from `base`: a PR whose merge-base predates the tdd pack is still checked. `base` only sets the
    diff. `head_ref` (the PR's branch) only adds a check; it grants nothing."""
    res = GuardResult()
    if not tl.has_ref(top, trusted):
        res.kind = "error"
        res.errors.append(f"{trusted} is not available; fetch it before classifying the PR")
        return res
    profile = tl.load_profile(top, trusted)
    if profile is None:
        res.kind = "bootstrap"
        res.notes.append(f"no {tl.PROFILE} on {trusted} yet: bootstrap PR, review it by hand")
        return res
    protected = tl.load_protected(top, trusted)
    files = changed_files(top, base, head)
    res.files = {f: classify_path(f, profile, protected) for f in files}
    _lock_status_changes_are_maintenance(top, head, trusted, res)
    classes = set(res.files.values())

    if MAINT in classes:
        res.kind = MAINT
        res.notes.append("maintenance PR: changes the tdd harness, workflows or gate settings; "
                         "review every line")
        if classes != {MAINT}:
            res.errors.append("mixed PR: maintenance paths must be changed in a PR of their own: "
                              + ", ".join(f for f, c in res.files.items() if c != MAINT))
        return res

    if SPEC in classes:
        res.kind = SPEC
        if tl.parse_branch(head_ref).kind == "impl":
            res.errors.append(f"spec tests, locks or protected fixtures changed from {head_ref}: "
                              "spec changes come only from a spec/<id> branch")
        others = [f for f, c in res.files.items() if c == IMPL]
        if others:
            res.errors.append("mixed PR: a spec PR may not change " + ", ".join(others))
        touched = {i for f, c in res.files.items() if c == SPEC and (i := tl.spec_dir_id(f, profile))}
        for cid in sorted(touched & tl.active_lock_ids(top, trusted)):
            if tl.lock_path(cid) not in res.files:
                res.errors.append(f"locked tests of {cid} changed without re-locking it "
                                  f"({tl.lock_path(cid)} is unchanged); run `tdd lock {cid}` on spec/{cid}")
        for f in (f for f, c in res.files.items() if c == SRC):
            res.errors += tl.stub_only_errors(tl.show(top, base, f), tl.show(top, head, f), f)
        for f in (f for f, c in res.files.items() if c == SPEC and tl.under(f, tl.LOCKS)):
            text = tl.show(top, head, f)
            if text is not None:
                try:
                    lock = json.loads(text)
                except ValueError:
                    res.errors.append(f"{f}: not valid JSON")
                    continue
                if lock.get("id") != Path(f).stem:
                    res.errors.append(f"{f}: lock id {lock.get('id')!r} does not match its file name")
                if tl.show(top, base, f) is not None:
                    res.notes.append(f"re-lock of {Path(f).stem}: review every changed locked test")
        return res

    res.kind = IMPL
    res.errors += _locked_signature_errors(top, trusted, head, set(files))
    return res


# =========================================================================== cli
VENDOR_FILES = ("tdd.py", "tddlib.py", "tdd_plugin.py")


def _top(args: argparse.Namespace) -> Path:
    top = tl.find_toplevel(args.repo or os.getcwd())
    if top is None:
        sys.exit("tdd: not inside a git repository")
    return top


def _profile(top: Path) -> tl.Profile:
    prof = tl.load_profile(top) or tl.load_profile(top, None, worktree_fallback=True)
    assert prof is not None
    if prof.source != tl.MAIN:
        print(f"note: using the {prof.source} profile; it is trusted only once on origin/main", file=sys.stderr)
    return prof


def _print_list(title: str, lines: list[str]) -> None:
    if lines:
        print(title)
        for ln in lines:
            print(f"  - {ln}")


def cmd_lint(a: argparse.Namespace) -> int:
    top = _top(a)
    res = lint(top, _profile(top), a.id)
    for v in res.violations:
        print(v)
    _print_list("red-exempt tags for review:", res.red_exempt())
    print("spec_lint: OK" if res.ok else f"spec_lint: {len(res.violations)} violation(s)")
    return 0 if res.ok else 1


def cmd_red(a: argparse.Namespace) -> int:
    top = _top(a)
    res = red_check(top, _profile(top), a.id)
    _print_list("problems:", res.problems)
    _print_list("notes:", res.notes)
    print("red_check: OK" if res.ok else "red_check: FAILED")
    return 0 if res.ok else 1


def cmd_retire(a: argparse.Namespace) -> int:
    """Mark a lock retired by record (history not in this repo, e.g. a published mirror).

    The change is a maintenance change (the path guard classifies it so): run it on a maintenance
    branch, never on spec/* or impl/*, and merge it in a PR of its own.
    """
    top = _top(a)
    branch = tl.parse_branch(tl.current_branch(top))
    if branch.kind in ("spec", "impl"):
        print(f"tdd retire: a maintenance change; run it on a maintenance branch, not {branch.name}",
              file=sys.stderr)
        return 2
    record = (a.record or "").strip()
    if not record:
        print("tdd retire: --record must say why the lock is retired (e.g. the merged PR)", file=sys.stderr)
        return 1
    path = top / tl.lock_path(a.id)
    if not path.exists():
        print(f"tdd retire: no lock for {a.id}", file=sys.stderr)
        return 1
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock.get("status") == tl.RETIRED:
        print(f"{a.id} is already retired")
        return 0
    lock["status"] = tl.RETIRED
    lock["retired"] = {"record": record, "date": dt.date.today().isoformat()}
    write_lock(top, lock)
    print(f"retired {a.id} by record. Commit it on its own and merge it as a maintenance PR.")
    return 0


def cmd_lock(a: argparse.Namespace) -> int:
    top = _top(a)
    branch = tl.parse_branch(tl.current_branch(top))
    if branch.kind != "spec" or branch.id != a.id:
        print(f"tdd lock: run on branch spec/{a.id} (current: {branch.name})", file=sys.stderr)
        return 2
    try:
        lock = build_lock(top, _profile(top), a.id)
    except LockRefused as e:
        _print_list("tdd lock refused:", e.problems)
        return 1
    path = write_lock(top, lock)
    print(f"wrote {path.relative_to(top).as_posix()}: {len(lock['nodes'])} tests, "
          f"{len(lock['interface'])} interface items. Commit it with the tests and open the spec PR.")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    top = _top(a)
    prof = _profile(top)
    res = run_tests(top, prof, [f"{prof.spec_root}/{a.id}"], hypothesis_profile=prof.gate_profile)
    if not res.report_ok:
        print(res.output[-4000:])
        return 1
    for nid in res.collected:
        rec = res.outcome(nid)
        print(f"{tl.classify(rec, a.id):5}  {nid}  {tl.describe(rec) if rec and rec.get('outcome') != 'passed' else ''}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    top = _top(a)
    tl.fetch_main(top)
    prof = tl.load_profile(top)
    if prof is None:
        print("no profile on origin/main")
        return 0
    integ = integrity(top, prof, tl.parse_branch(tl.current_branch(top)))
    _print_list("integrity problems:", integ.problems)
    _print_list("rebase needed for:", integ.rebase)
    _print_list("warnings:", integ.warnings)
    print("integrity: OK" if not integ.problems else "integrity: FAILED")
    return 0 if not integ.problems else 1


def cmd_gate(a: argparse.Namespace) -> int:
    top = _top(a)
    os.environ.setdefault("TDD_HISTORY_DIR", str(Path(os.environ.get("TEMP", "/tmp")) / "tdd-history-dryrun"))
    state_dir = top / tl.STATE
    backup = {p: p.read_bytes() for p in state_dir.glob("*.json")} if state_dir.is_dir() else {}
    try:
        decision = stop(top)
    finally:                                                 # a dry run leaves the counters alone
        for p in state_dir.glob("*.json") if state_dir.is_dir() else []:
            if p not in backup:
                p.unlink()
        for p, data in backup.items():
            p.write_bytes(data)
    print(json.dumps(decision or {"decision": "allow"}, indent=2))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    top = _top(a)
    res = verify(top, _profile(top), a.id)
    _print_list("failures:", res.failures)
    _print_list("notes:", res.notes)
    if res.dispute:
        print(f"wrote {res.dispute.relative_to(top).as_posix()}")
    print("verify: OK" if res.ok else "verify: FAILED")
    return 0 if res.ok else 1


def cmd_status(a: argparse.Namespace) -> int:
    top = _top(a)
    fetched, note = tl.fetch_main(top)
    prof = tl.load_profile(top)
    print(f"tools {tl.VERSION}; branch {tl.current_branch(top)}; {note or 'origin/main fetched'}")
    if prof is None:
        print("no profile on origin/main")
        return 0
    for cid in tl.lock_ids(top):
        lock = tl.read_lock(top, cid) or {}
        status = "retired" if tl.is_retired(top, cid) else lock.get("status", "?")
        cache = {k: tl.load_state(top, f"{k}-{cid}").get("status") for k in ("spec", "impl")}
        warn = "  WARNING: implemented but retirement not recognised" \
            if status == "active" and unrecognised_retirement(top, prof, cid, lock) else ""
        print(f"  {cid:30} {status:10} {len(lock.get('nodes', []))} tests  cache={cache or '-'}{warn}")
    installed = Path(os.environ.get("TDD_MANAGED_DIR", r"C:\Program Files\ClaudeCode\tdd")) / "tddlib.py"
    if installed.exists():
        m = re.search(r'^VERSION = "([^"]+)"', installed.read_text(encoding="utf-8"), re.M)
        if m and m.group(1) != tl.VERSION:
            print(f"managed install is {m.group(1)}, these tools are {tl.VERSION}: reinstall the hooks")
    return 0


def cmd_path_guard(a: argparse.Namespace) -> int:
    top = _top(a)
    res = path_guard(top, a.base, a.head, a.head_ref)
    print(f"PR kind: {res.kind}")
    for f, c in sorted(res.files.items()):
        print(f"  {c:15} {f}")
    _print_list("notes:", res.notes)
    _print_list("errors:", res.errors)
    return 0 if res.ok else 1


def cmd_ci(a: argparse.Namespace) -> int:
    top = _top(a)
    res = run_ci(top, a.head_ref or "")
    _print_list("summary:", res.summary)
    _print_list("errors:", res.errors)
    return 0 if res.ok else 1


def _manifest(folder: Path) -> dict[str, str]:
    return {name: tl.sha256_file(folder / name) for name in VENDOR_FILES if (folder / name).exists()}


# Pack files `tdd init` adds when missing. All are maintenance paths: review them in the init PR.
PROFILE_TEMPLATE = """\
# .claude/tdd/profile.toml: the gate's settings for this repo (a maintenance path).
# The gate reads this file from origin/main, never from the working tree. Unknown keys are an error.
spec_root = "{spec_root}"
unit_root = "{unit_root}"
dispute_dir = ".claude/tdd/disputes"
specs_dir = "docs/specs"
src_roots = ["src"]               # where interface stubs live
pythonpath = ["src", "."]         # the only PYTHONPATH the controlled run gets
plugins = ["hypothesispytest"]    # pytest plugins enabled with -p (autoload is off)
budget = 8                        # blocked stops per change before the gate gives way
valuation_date = ""               # the fixed "today" spec tests use, e.g. "2026-05-29"

[hypothesis]
gate_profile = "tdd-gate"         # registered in <spec_root>/conftest.py
verify_profile = "tdd-verify"
"""

PROTECTED_TEMPLATE = """\
# Globs (repo-relative) protected like spec tests: changed only on spec/<id> branches.
# Golden sets and shared fixtures, e.g.:
# test/eval/golden/**
"""

SPEC_CONFTEST_TEMPLATE = '''\
"""<spec_root>/conftest.py: shared by every spec test (a maintenance path).

Registers the hypothesis profiles the gate and `tdd verify` select. The gate's profile is
reproducible and keeps no example database (which ignores -p no:cacheprovider).
"""

from hypothesis import settings

settings.register_profile("tdd-gate", derandomize=True, database=None, deadline=None, print_blob=True)
settings.register_profile("tdd-verify", derandomize=False, database=None, deadline=None,
                          max_examples=1000, print_blob=True)
'''

PATH_GUARD_WORKFLOW = """\
# tdd-path-guard: classify each PR by the files it changes (spec / maintenance / implementation).
# pull_request_target: GitHub always uses main's copy of this workflow and main's tools/tdd. The PR
# is fetched as git objects only, never checked out or executed. The PR that first adds this file
# is not checked by it: review that PR by hand.
name: tdd-path-guard
on:
  pull_request_target:
    branches: [main]
permissions:
  contents: read
  pull-requests: read      # retirement: GitHub's record of merged impl/<id> PRs
jobs:
  path-guard:
    runs-on: ubuntu-latest
    steps:
      - name: Check out main (never the PR)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Fetch the PR head as git objects only
        run: git fetch --no-tags origin "+refs/pull/${{ github.event.pull_request.number }}/head:refs/remotes/pr/head"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Verify the tdd tools on main
        run: python tools/tdd/tdd.py manifest-check
      - name: Classify the PR
        env:
          HEAD_REF: ${{ github.head_ref }}
          GH_TOKEN: ${{ github.token }}
        run: |
          base=$(git merge-base origin/main refs/remotes/pr/head)
          python tools/tdd/tdd.py path-guard --base "$base" --head refs/remotes/pr/head --head-ref "$HEAD_REF"
"""

SPEC_WORKFLOW = """\
# tdd-spec: run every spec test with the controlled run. Graduated tests must pass; active-lock
# tests must pass or fail for the right reason; on the PR from exactly impl/<id>, that lock's tests
# must all pass. The tools are taken from main, not from the PR.
name: tdd-spec
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
permissions:
  contents: read
  pull-requests: read      # retirement: GitHub's record of merged impl/<id> PRs
jobs:
  spec-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pytest hypothesis
          # PROJECT: install the project's own dependencies here (e.g. pip install -r requirements.txt).
      - name: Take the tdd tools from main
        run: |
          git fetch --no-tags origin main
          mkdir -p "$RUNNER_TEMP/tdd-main"
          git archive origin/main tools/tdd | tar -x -C "$RUNNER_TEMP/tdd-main"
          python "$RUNNER_TEMP/tdd-main/tools/tdd/tdd.py" manifest-check
      - name: Spec and regression tests
        env:
          HEAD_REF: ${{ github.head_ref }}
          GH_TOKEN: ${{ github.token }}
        run: python "$RUNNER_TEMP/tdd-main/tools/tdd/tdd.py" ci --head-ref "$HEAD_REF"
"""


def cmd_init(a: argparse.Namespace) -> int:
    """Vendor the tools (always refreshed) and add pack files that don't exist yet."""
    repo = Path(a.target).resolve()
    dest = repo / "tools" / "tdd"
    dest.mkdir(parents=True, exist_ok=True)
    for name in VENDOR_FILES:
        shutil.copy2(HERE / name, dest / name)
    (dest / "MANIFEST.json").write_text(json.dumps({"version": tl.VERSION, "files": _manifest(dest)},
                                                   indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"vendored tdd tools {tl.VERSION} into {dest}")
    pack = {
        tl.PROFILE: PROFILE_TEMPLATE.format(spec_root=a.spec_root, unit_root=a.unit_root),
        tl.PROTECTED: PROTECTED_TEMPLATE,
        f"{a.spec_root}/conftest.py": SPEC_CONFTEST_TEMPLATE,
        ".github/workflows/tdd-path-guard.yml": PATH_GUARD_WORKFLOW,
        ".github/workflows/tdd-spec.yml": SPEC_WORKFLOW,
    }
    for rel, text in pack.items():
        p = repo / rel
        if p.exists():
            print(f"kept existing {rel}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        print(f"added {rel}")
    return 0


def cmd_manifest_check(a: argparse.Namespace) -> int:
    data = json.loads((HERE / "MANIFEST.json").read_text(encoding="utf-8"))
    expected, actual = data.get("files", {}), _manifest(HERE)
    bad = sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))
    _print_list("files differing from MANIFEST.json:", bad)
    if data.get("version") != tl.VERSION:
        bad.append("version")
        print(f"MANIFEST.json version {data.get('version')} != tools {tl.VERSION}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tdd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("lint", cmd_lint), ("red", cmd_red), ("lock", cmd_lock), ("run", cmd_run),
                     ("verify", cmd_verify)):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(fn=fn)
    for name, fn in (("check", cmd_check), ("gate", cmd_gate), ("status", cmd_status),
                     ("manifest-check", cmd_manifest_check)):
        sub.add_parser(name).set_defaults(fn=fn)
    p = sub.add_parser("retire")
    p.add_argument("id")
    p.add_argument("--record", required=True)
    p.set_defaults(fn=cmd_retire)
    p = sub.add_parser("path-guard")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--head-ref", default="")
    p.set_defaults(fn=cmd_path_guard)
    p = sub.add_parser("ci")
    p.add_argument("--head-ref", default="")
    p.set_defaults(fn=cmd_ci)
    p = sub.add_parser("init")
    p.add_argument("target")
    p.add_argument("--spec-root", default="tests/spec")
    p.add_argument("--unit-root", default="tests/unit")
    p.set_defaults(fn=cmd_init)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
