"""The full cycle on a scratch project: spec, lint/red, lock, merge, implement, retire,
CI, and the CLI as CI runs it (§14.1 checks 10-19)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import tdd
import tddlib as tl
from conftest import BONDS, IMPL, SKILL, git, implement, merge_to_main, write



# =========================================================================== workflow
def _profile(repo: Path) -> tl.Profile:
    prof = tl.load_profile(repo)
    assert prof is not None
    return prof


# --------------------------------------------------------------------------- spec branch
def test_lint_and_red_check_pass_for_good_tests(spec_branch: Path) -> None:
    prof = _profile(spec_branch)
    lint = tdd.lint(spec_branch, prof, "c1")
    assert lint.ok, [str(v) for v in lint.violations]
    red = tdd.red_check(spec_branch, prof, "c1", lint_res=lint)
    assert red.ok, red.problems
    assert [i["qualname"] for i in red.items] == ["zero_price"]


def test_red_check_rejects_isinstance_only_test(spec_branch: Path) -> None:
    """Check 16: a test that passes against any float-returning implementation is rejected."""
    write(spec_branch, "tests/spec/c1/test_weak.py",
          "import pytest\nfrom pricing.bonds import zero_price\n\n"
          "@pytest.mark.criterion('C1')\ndef test_type():\n    # oracle: type only\n"
          "    assert isinstance(zero_price(1.0, 0.0, 1.0), float)\n")
    red = tdd.red_check(spec_branch, _profile(spec_branch), "c1")
    assert any("test_weak.py::test_type" in p and "(zero)" in p for p in red.problems), red.problems


def test_red_check_rejects_test_passing_on_current_code(spec_branch: Path) -> None:
    write(spec_branch, "tests/spec/c1/test_trivial.py",
          "import pytest\n\n@pytest.mark.criterion('C1')\ndef test_x():\n    # oracle: none\n    assert True\n")
    red = tdd.red_check(spec_branch, _profile(spec_branch), "c1")
    assert any("test_trivial.py::test_x passes against the current code" in p for p in red.problems)


def test_red_check_rejects_wrong_reason(spec_branch: Path) -> None:
    write(spec_branch, "tests/spec/c1/test_typo.py",
          "import pytest\n\n@pytest.mark.criterion('C1')\ndef test_x():\n    # oracle: none\n"
          "    raise KeyError('typo')\n")
    red = tdd.red_check(spec_branch, _profile(spec_branch), "c1")
    assert any("test_typo.py::test_x fails for the wrong reason" in p and "KeyError" in p
               for p in red.problems)


def test_red_exempt_allows_a_variant(spec_branch: Path) -> None:
    write(spec_branch, "tests/spec/c1/test_refusal.py",
          "import pytest\nfrom pricing.bonds import zero_price\n\n"
          "@pytest.mark.criterion('C1')\ndef test_never_negative():\n"
          "    # oracle: prices are non-negative\n    # red-exempt: zero  zero is a valid lower bound here\n"
          "    assert zero_price(1.0, 0.01, 1.0) >= 0\n")
    red = tdd.red_check(spec_branch, _profile(spec_branch), "c1")
    assert not any("test_refusal" in p and "(zero)" in p for p in red.problems)
    assert any("test_refusal" in n and "exempted" in n for n in red.notes)


def test_lock_contents(spec_branch: Path) -> None:
    lock = tdd.build_lock(spec_branch, _profile(spec_branch), "c1")
    assert lock["status"] == "active"
    assert lock["nodes"] == ["tests/spec/c1/test_zero_price.py::test_known_value",
                             "tests/spec/c1/test_zero_price.py::test_linear_in_face"]
    assert lock["criteria"]["C1"] == lock["nodes"]
    assert lock["interface"][0]["signature"] == "def zero_price(face: float, rate: float, years: float) -> float"
    assert "conftest.py" in lock["absent"] and "tests/conftest.py" in lock["absent"]
    assert "tests/spec/conftest.py" not in lock["absent"]          # maintenance path, checked vs main
    assert "src/sitecustomize.py" in lock["absent"]


def test_lock_refused_on_lint_violation(spec_branch: Path) -> None:
    write(spec_branch, "tests/spec/c1/test_bad.py", "def test_x():\n    assert 0.1 + 0.2 == 0.3\n")
    try:
        tdd.build_lock(spec_branch, _profile(spec_branch), "c1")
    except tdd.LockRefused as e:
        assert any("float-eq" in p for p in e.problems) and any("[oracle]" in p for p in e.problems)
    else:
        raise AssertionError("lock was not refused")


# --------------------------------------------------------------------------- implementation
def test_impl_gate_blocks_until_green(locked: Path) -> None:
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "not passing yet" in d["reason"]
    implement(locked)
    assert tdd.stop(locked) is None
    assert tl.load_state(locked, "impl-c1")["status"] == "green"


def test_locked_test_edit_is_tampering(locked: Path) -> None:
    """Check 10/11: changing a locked test on the impl branch blocks with restore instructions."""
    p = locked / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("rel=1e-12", "rel=1e-1"), encoding="utf-8")
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "test_zero_price.py: changed" in d["reason"]
    assert "git restore --source=origin/main" in d["reason"]


def test_test_and_lock_changed_together_is_caught(locked: Path) -> None:
    """Check 11: rewriting the test and its lock on the branch doesn't pass; the lock comes from main."""
    p = locked / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("rel=1e-12", "rel=1e-1"), encoding="utf-8")
    lp = locked / tl.lock_path("c1")
    lock = json.loads(lp.read_text(encoding="utf-8"))
    lock["files"] = tl.tree_files(locked, "tests/spec/c1")
    lp.write_text(json.dumps(lock), encoding="utf-8")
    git(locked, "commit", "-qam", "loosen")
    integ = tdd.integrity(locked, _profile(locked), tl.parse_branch("impl/c1"))
    assert any("test_zero_price.py: changed" in p for p in integ.problems)
    res = tdd.path_guard(locked, "origin/main", "HEAD", "impl/c1")
    assert not res.ok and res.kind == "spec"
    assert any("from impl/c1" in e for e in res.errors)
    assert any("re-lock of c1" in n for n in res.notes)


def test_gate_bypass_attempts_fail(locked: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Check 12: root conftest, PYTEST_ADDOPTS, a skip, a branch plugin, a branch gate ini, a signature."""
    implement(locked)
    prof = _profile(locked)
    branch = tl.parse_branch("impl/c1")

    write(locked, "conftest.py", "import pytest\n")
    assert any("conftest.py: must not exist" in p for p in tdd.integrity(locked, prof, branch).problems)
    (locked / "conftest.py").unlink()

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches")
    monkeypatch.setenv("PYTEST_PLUGINS", "rerunfailures")
    assert tdd.stop(locked) is None                         # environment is reduced: still green
    monkeypatch.delenv("PYTEST_ADDOPTS")
    monkeypatch.delenv("PYTEST_PLUGINS")

    write(locked, ".claude/tdd/profile.toml", "plugins = ['bogus_plugin']\n" + (locked / ".claude/tdd/profile.toml")
          .read_text(encoding="utf-8").replace("plugins = []\n", ""))
    write(locked, "pytest.ini", "[pytest]\naddopts = -k nothing\n")
    assert tdd.stop(locked) is None                         # both are read from origin/main
    git(locked, "checkout", "-q", "--", ".claude/tdd/profile.toml")
    (locked / "pytest.ini").unlink()

    src = locked / "src/pricing/bonds.py"
    src.write_text(src.read_text(encoding="utf-8").replace("years: float) -> float", "years: float = 1.0) -> float"),
                   encoding="utf-8")
    assert any("signature of zero_price changed" in p for p in tdd.integrity(locked, prof, branch).problems)


def test_skip_added_fails(locked: Path) -> None:
    implement(locked)
    p = locked / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("def test_known_value", "@pytest.mark.skip\ndef test_known_value"),
                 encoding="utf-8")
    res = tdd.run_tests(locked, _profile(locked), ["tests/spec/c1"])
    failing, wrong = tdd.judge(res, "c1", tl.read_lock(locked, "c1") or {})
    assert any("test_known_value" in w and "skipped" in w for w in wrong)


def test_branch_cut_before_lock_is_not_tampering(project: Path) -> None:
    """Check 13."""
    from conftest import SPEC, STUB, TESTS, commit_all
    git(project, "branch", "-q", "feature/old")
    git(project, "checkout", "-q", "-b", "spec/c1")
    write(project, "docs/specs/c1.md", SPEC)
    (project / "src/pricing/bonds.py").write_text(BONDS + STUB, encoding="utf-8")
    write(project, "tests/spec/c1/test_zero_price.py", TESTS)
    prof = _profile(project)
    tdd.write_lock(project, tdd.build_lock(project, prof, "c1"))
    commit_all(project, "test(spec): c1")
    merge_to_main(project, "spec/c1", 1)
    git(project, "checkout", "-q", "feature/old")
    integ = tdd.integrity(project, prof, tl.parse_branch("feature/old"))
    assert integ.problems == [] and integ.active == {}


def test_relock_requires_rebase(locked: Path) -> None:
    """Check 14: a re-lock merged on main blocks the un-rebased impl branch with 'rebase'."""
    from conftest import commit_all
    git(locked, "checkout", "-q", "main")
    git(locked, "branch", "-q", "-D", "spec/c1")
    git(locked, "checkout", "-q", "-b", "spec/c1")
    p = locked / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("(1.0, 50.0, 250.0)", "(1.0, 50.0, 250.0, 1000.0)"),
                 encoding="utf-8")
    tdd.write_lock(locked, tdd.build_lock(locked, _profile(locked), "c1"))
    commit_all(locked, "test(spec): c1 re-lock")
    merge_to_main(locked, "spec/c1", 2)
    git(locked, "checkout", "-q", "impl/c1")
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "rebase impl/c1" in d["reason"]


def test_budget_gives_way(locked: Path) -> None:
    for _ in range(3):
        assert tdd.stop(locked)["decision"] == "block"  # type: ignore[index]
    d = tdd.stop(locked)
    assert d and "decision" not in d and "budget" in d["systemMessage"]
    assert tl.load_state(locked, "impl-c1")["status"] == "budget-exhausted"


def test_dispute_allows_stop(locked: Path) -> None:
    write(locked, ".claude/tdd/disputes/c1.md", "The oracle is wrong because ...\n")
    d = tdd.stop(locked)
    assert d and "dispute open" in d["systemMessage"]


def test_part_branch_needs_only_right_reason(locked: Path) -> None:
    git(locked, "checkout", "-q", "-b", "impl/c1--part1")
    assert tdd.stop(locked) is None                         # NotImplementedError is the right reason
    src = locked / "src/pricing/bonds.py"
    src.write_text(BONDS + IMPL.replace("return face * discount_factor(rate, years)", "return {}['x']"),
                   encoding="utf-8")
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "wrong reason" in d["reason"]


# --------------------------------------------------------------------------- CI and retirement
def test_ci_expected_red_and_must_pass(locked: Path) -> None:
    """Check 18."""
    git(locked, "checkout", "-q", "main")
    assert tdd.run_ci(locked, "").ok                          # main stays green with an active lock
    assert tdd.run_ci(locked, "feature/x").ok                 # an unrelated PR is not red
    git(locked, "checkout", "-q", "impl/c1")
    res = tdd.run_ci(locked, "impl/c1")
    assert not res.ok                                        # the impl PR must turn it green
    implement(locked)
    assert tdd.run_ci(locked, "impl/c1").ok


def test_retirement_from_merge_subject(locked: Path) -> None:
    """Check 19 (synthetic): only an exact impl/<id> merge subject retires the lock."""
    from conftest import commit_all
    git(locked, "checkout", "-q", "-b", "impl/c1--part1")
    write(locked, "src/pricing/notes.py", "# groundwork for c1\n")
    commit_all(locked, "part 1")
    merge_to_main(locked, "impl/c1--part1", 2)
    assert not tl.is_retired(locked, "c1")
    git(locked, "checkout", "-q", "impl/c1")
    git(locked, "merge", "-q", "main")
    implement(locked)
    commit_all(locked, "implement c1")
    merge_to_main(locked, "impl/c1", 3)
    assert tl.is_retired(locked, "c1")
    git(locked, "checkout", "-q", "main")
    res = tdd.run_ci(locked, "")
    assert res.ok and any("graduated" in s for s in res.summary)


def test_unrecognised_retirement_warns(locked: Path) -> None:
    from conftest import commit_all
    implement(locked)
    commit_all(locked, "implement c1")
    git(locked, "checkout", "-q", "main")
    git(locked, "merge", "-q", "--squash", "impl/c1")
    git(locked, "commit", "-q", "-m", "Implement c1 (#3)")
    git(locked, "push", "-q", "origin", "main")
    integ = tdd.integrity(locked, _profile(locked), tl.parse_branch("main"))
    assert any("no merge from impl/c1" in w for w in integ.warnings)


def test_protected_fixture_edit_on_impl_is_tampering(locked: Path) -> None:
    write(locked, "tests/golden/curve.json", '{"10y": 0.05}\n')
    integ = tdd.integrity(locked, _profile(locked), tl.parse_branch("impl/c1"))
    assert any("tests/golden/curve.json: differs" in p for p in integ.problems)


def test_fetch_finds_new_lock_and_reports_age_offline(project: Path, tmp_path: Path) -> None:
    """Check 15: a lock merged on origin but not yet fetched is found; offline, the ref's age is shown."""
    from conftest import SPEC, STUB, TESTS, commit_all
    import subprocess
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True, capture_output=True)
    git(other, "checkout", "-q", "-b", "spec/c1")
    write(other, "docs/specs/c1.md", SPEC)
    (other / "src/pricing/bonds.py").write_text(BONDS + STUB, encoding="utf-8")
    write(other, "tests/spec/c1/test_zero_price.py", TESTS)
    tdd.write_lock(other, tdd.build_lock(other, _profile(other), "c1"))
    commit_all(other, "test(spec): c1")
    merge_to_main(other, "spec/c1", 1)                       # merged "on GitHub"; project hasn't fetched
    git(project, "checkout", "-q", "-b", "impl/c1", "origin/main")
    assert tl.read_lock(project, "c1") is None
    git(project, "pull", "-q", "origin", "main")
    d = tdd.stop(project)
    assert d and d["decision"] == "block"                    # the gate fetched and found the lock
    git(project, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    fetched, note = tl.fetch_main(project)
    assert not fetched and "could not fetch origin/main" in note and "last updated" in note


def test_unit_tests_untouched_by_gate(locked: Path) -> None:
    """The gate runs only the change's spec tests; a failing unit test is CI's existing business."""
    implement(locked)
    write(locked, "tests/unit/test_df.py", "def test_df():\n    assert False\n")
    assert tdd.stop(locked) is None
    _ = IMPL


# =========================================================================== cli
def _tdd(tool_dir: Path, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(tool_dir / "tdd.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, env={**os.environ})


def test_init_and_manifest(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    r = subprocess.run([sys.executable, str(SKILL / "scripts" / "tdd.py"), "init", str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    tools = target / "tools" / "tdd"
    assert (tools / "MANIFEST.json").exists() and (tools / "tdd_plugin.py").exists()
    assert (target / ".claude/tdd/profile.toml").exists() and (target / ".github/workflows/tdd-spec.yml").exists()
    assert (target / "tests/spec/conftest.py").exists()
    check = subprocess.run([sys.executable, str(tools / "tdd.py"), "manifest-check"], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout
    with (tools / "tddlib.py").open("a", encoding="utf-8") as f:
        f.write("\n# tampered\n")
    check = subprocess.run([sys.executable, str(tools / "tdd.py"), "manifest-check"], capture_output=True, text=True)
    assert check.returncode == 1 and "tddlib.py" in check.stdout


def test_ci_from_vendored_copy(locked: Path, tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(SKILL / "scripts" / "tdd.py"), "init", str(tmp_path / "main")],
                   check=True, capture_output=True)
    tools = tmp_path / "main" / "tools" / "tdd"
    r = _tdd(tools, locked, "ci", "--head-ref", "impl/c1")
    assert r.returncode == 1 and "c1" in r.stdout
    implement(locked)
    r = _tdd(tools, locked, "ci", "--head-ref", "impl/c1")
    assert r.returncode == 0, r.stdout + r.stderr


def test_status_and_check(locked: Path) -> None:
    tools = SKILL / "scripts"
    r = _tdd(tools, locked, "status")
    assert r.returncode == 0 and "c1" in r.stdout and "active" in r.stdout
    r = _tdd(tools, locked, "check")
    assert r.returncode == 0 and "integrity: OK" in r.stdout


# =========================================================================== review fixes
FORGED_PLUGIN = '''\
import json, os

def pytest_sessionfinish(session, exitstatus):
    ids = [i.nodeid for i in session.items]
    with open(os.environ["TDD_REPORT"], "w") as f:
        json.dump({"collected": ids, "results": {n: {"outcome": "passed", "when": "call"} for n in ids},
                   "exitstatus": 0}, f)
'''

FORGED_PYTEST = '''\
import json, os, sys

def main(args=None):
    with open(os.environ["TDD_REPORT"], "w") as f:
        json.dump({"collected": [], "results": {}, "exitstatus": 0}, f)
    return 0
'''


def test_repo_root_plugin_cannot_shadow_the_gate_plugin(locked: Path) -> None:
    """P1: a repo-root tdd_plugin.py must not replace the gate's plugin and forge an all-pass report."""
    write(locked, "tdd_plugin.py", FORGED_PLUGIN)
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "not passing yet" in d["reason"]


def test_project_modules_cannot_shadow_pytest_or_stdlib(locked: Path) -> None:
    """P1: modules on the profile's pythonpath (src/) can't shadow pytest or the standard library."""
    write(locked, "src/pytest.py", FORGED_PYTEST)
    write(locked, "src/json.py", "raise SystemExit('shadowed json')\n")
    prof = _profile(locked)
    res = tdd.run_tests(locked, prof, ["tests/spec/c1"])
    assert res.report_ok and len(res.collected) == 2, res.output
    assert all(tl.classify(res.outcome(n), "c1") == tl.RIGHT for n in res.collected)


def test_ci_runs_lock_integrity(locked: Path) -> None:
    """P2: CI catches what the local gate catches: absence records, signatures."""
    implement(locked)
    write(locked, "src/sitecustomize.py", "import builtins\n")
    res = tdd.run_ci(locked, "impl/c1")
    assert not res.ok and any("sitecustomize.py: must not exist" in e for e in res.errors), res.errors
    (locked / "src/sitecustomize.py").unlink()
    src = locked / "src/pricing/bonds.py"
    src.write_text(src.read_text(encoding="utf-8").replace("years: float)", "years: float, k: int = 0)"),
                   encoding="utf-8")
    res = tdd.run_ci(locked, "feature/not-impl")
    assert any("signature of zero_price changed" in e for e in res.errors), res.errors


def test_spec_budget_does_not_disable_impl_gate(spec_branch: Path) -> None:
    """P2: budget and status are kept per role, so a spec-phase exhaustion can't switch off the impl gate."""
    from conftest import commit_all
    write(spec_branch, "tests/spec/c1/test_bad.py", "def test_x():\n    assert 0.1 == 0.1\n")
    for _ in range(4):
        tdd.stop(spec_branch)
    assert tl.load_state(spec_branch, "spec-c1")["status"] == "budget-exhausted"
    (spec_branch / "tests/spec/c1/test_bad.py").unlink()
    tdd.write_lock(spec_branch, tdd.build_lock(spec_branch, _profile(spec_branch), "c1"))
    commit_all(spec_branch, "test(spec): c1")
    merge_to_main(spec_branch, "spec/c1", 1)
    git(spec_branch, "checkout", "-q", "-b", "impl/c1")
    d = tdd.stop(spec_branch)
    assert d and d["decision"] == "block" and "not passing yet" in d["reason"]


def test_src_that_reaches_into_the_gate_is_flagged(locked: Path) -> None:
    """P1 (in-process variant): project code runs inside the test process, so it could tamper with the
    report. That can't be prevented, only detected: src referring to gate internals or importing
    pytest/hypothesis fails the gate and CI."""
    implement(locked)
    write(locked, "src/pricing/hack.py", "import os\nREPORT = os.environ.get('TDD_REPORT')\n")
    d = tdd.stop(locked)
    assert d and d["decision"] == "block" and "src/pricing/hack.py" in d["reason"], d
    res = tdd.run_ci(locked, "impl/c1")
    assert any("src/pricing/hack.py" in e for e in res.errors), res.errors
    (locked / "src/pricing/hack.py").unlink()
    write(locked, "src/pricing/hack2.py", "import _pytest.runner\n")
    assert any("src/pricing/hack2.py" in e for e in tdd.run_ci(locked, "impl/c1").errors)


def test_spec_branch_integrity_and_ci_cover_other_changes(locked: Path) -> None:
    """On spec/c2, c1's locked tests are still hash-checked (locally and in CI), unless the PR re-locks c1."""
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c2")
    p = locked / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("rel=1e-12", "rel=1e-3"), encoding="utf-8")
    integ = tdd.integrity(locked, _profile(locked), tl.parse_branch("spec/c2"))
    assert any("[c1]" in x and "test_zero_price.py: changed" in x for x in integ.problems), integ.problems
    assert any("[c1]" in e and "changed" in e for e in tdd.run_ci(locked, "spec/c2").errors)


def test_ci_checks_a_spec_prs_own_lock(spec_branch: Path) -> None:
    """A test edited after `tdd lock` (lock not refreshed) fails CI and the spec-branch gate."""
    from conftest import commit_all
    tdd.write_lock(spec_branch, tdd.build_lock(spec_branch, _profile(spec_branch), "c1"))
    p = spec_branch / "tests/spec/c1/test_zero_price.py"
    p.write_text(p.read_text(encoding="utf-8").replace("(1.0, 50.0, 250.0)", "(1.0, 50.0)"), encoding="utf-8")
    commit_all(spec_branch, "test(spec): c1, edited after locking")
    res = tdd.run_ci(spec_branch, "spec/c1")
    assert any("[c1]" in e and "test_zero_price.py: changed" in e for e in res.errors), res.errors
    d = tdd.stop(spec_branch)
    assert d and d["decision"] == "block" and "tdd lock c1" in d["reason"], d


def test_spec_root_helpers_are_integrity_checked(locked: Path) -> None:
    write(locked, "tests/spec/helpers.py", "TOL = 1.0\n")
    integ = tdd.integrity(locked, _profile(locked), tl.parse_branch("impl/c1"))
    assert any("tests/spec/helpers.py: added" in x for x in integ.problems), integ.problems


def test_non_ascii_protected_file_is_compared(project: Path) -> None:
    from conftest import commit_all
    write(project, "tests/golden/köln.json", "{}\n")
    commit_all(project, "golden with umlaut")
    git(project, "push", "-q", "origin", "main")
    git(project, "checkout", "-q", "-b", "impl/x")
    write(project, "tests/golden/köln.json", '{"tampered": 1}\n')
    integ = tdd.integrity(project, _profile(project), tl.parse_branch("impl/x"))
    assert any("köln.json: differs" in x for x in integ.problems), integ.problems


def test_worktree_glob_skips_ignored_folders(project: Path) -> None:
    write(project, ".gitignore", ".claude/tdd/state/\n__pycache__/\nnode_modules/\n")
    write(project, "node_modules/pkg/tests/golden/x.json", "{}\n")
    write(project, "tests/golden/new.json", "{}\n")
    hits = tl.worktree_glob(project, ["**/golden/*.json"])
    assert "tests/golden/new.json" in hits and "tests/golden/curve.json" in hits
    assert not any(h.startswith("node_modules/") for h in hits), hits


def test_integrity_sees_gitignored_maintenance_and_protected_files(locked: Path) -> None:
    """Ignoring a file (via .gitignore or .git/info/exclude) must not hide it from the comparison with main."""
    exclude = locked / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("tests/spec/evil_helper.py\ntests/golden/extra.json\n", encoding="utf-8")
    write(locked, "tests/spec/evil_helper.py", "TOL = 1.0\n")
    write(locked, "tests/golden/extra.json", "{}\n")
    probs = tdd.integrity(locked, _profile(locked), tl.parse_branch("impl/c1")).problems
    assert any("tests/spec/evil_helper.py: added" in p for p in probs), probs
    assert any("tests/golden/extra.json: added" in p for p in probs), probs


# =========================================================================== retirement without the default title
def _merge_custom(repo: Path, branch: str, title: str = "looks good") -> None:
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", title)
    git(repo, "push", "-q", "origin", "main")


def _implemented_and_pushed(locked: Path) -> None:
    from conftest import commit_all
    implement(locked)
    commit_all(locked, "implement c1")
    git(locked, "push", "-q", "origin", "impl/c1")


def test_retired_by_second_parent_with_custom_title(locked: Path) -> None:
    """A custom merge title still retires the lock when origin/impl/<id> is the merge's second parent."""
    _implemented_and_pushed(locked)
    _merge_custom(locked, "impl/c1")
    assert tl.is_retired(locked, "c1")


def test_part_branch_does_not_retire_by_second_parent(locked: Path) -> None:
    from conftest import commit_all
    git(locked, "checkout", "-q", "-b", "impl/c1--part1")
    write(locked, "src/pricing/notes.py", "# part 1\n")
    commit_all(locked, "part 1")
    git(locked, "push", "-q", "origin", "impl/c1--part1")
    _merge_custom(locked, "impl/c1--part1")
    assert not tl.is_retired(locked, "c1")


def test_retired_via_github_when_branch_is_deleted(locked: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Branch deleted after merge: GitHub's record of the merged PR (head ref exactly impl/<id>) decides."""
    _implemented_and_pushed(locked)
    _merge_custom(locked, "impl/c1")
    merge_sha = git(locked, "rev-parse", "origin/main").strip()
    git(locked, "push", "-q", "origin", "--delete", "impl/c1")
    git(locked, "fetch", "-q", "--prune", "origin")
    tl.clear_retirement_cache(locked)
    monkeypatch.setattr(tl, "github_pulls", lambda top, branch: None)          # offline / no gh
    assert not tl.is_retired(locked, "c1")
    tl.clear_retirement_cache(locked)
    pulls = [{"head": {"ref": "impl/c1--part1"}, "merged_at": "2026-10-04T00:00:00Z", "merge_commit_sha": merge_sha},
             {"head": {"ref": "impl/c1"}, "merged_at": None, "merge_commit_sha": merge_sha}]
    monkeypatch.setattr(tl, "github_pulls", lambda top, branch: pulls)
    assert not tl.is_retired(locked, "c1")                                  # part branch / not merged
    tl.clear_retirement_cache(locked)
    pulls.append({"head": {"ref": "impl/c1"}, "merged_at": "2026-10-04T00:00:00Z", "merge_commit_sha": merge_sha})
    assert tl.is_retired(locked, "c1")
    monkeypatch.setattr(tl, "github_pulls", lambda top, branch: None)
    assert tl.is_retired(locked, "c1")                                      # positive result is cached


def test_github_merge_must_be_on_main(locked: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tl.clear_retirement_cache(locked)
    git(locked, "commit", "-q", "--allow-empty", "-m", "unmerged")
    not_on_main = git(locked, "rev-parse", "HEAD").strip()                  # an impl commit, never merged
    monkeypatch.setattr(tl, "github_pulls", lambda top, branch: [
        {"head": {"ref": "impl/c1"}, "merged_at": "2026-10-04T00:00:00Z", "merge_commit_sha": not_on_main}])
    assert not tl.is_retired(locked, "c1")


def test_github_repo_from_origin_url() -> None:
    for url in ("https://github.com/acme/widgets.git", "git@github.com:acme/widgets.git",
                "ssh://git@github.com/acme/widgets", "https://github.com/acme/widgets"):
        assert tl.github_repo_from_url(url) == ("acme", "widgets"), url
    assert tl.github_repo_from_url("D:/repos/origin.git") is None


def test_init_workflows_can_read_merged_prs(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(SKILL / "scripts" / "tdd.py"), "init", str(tmp_path)], check=True,
                   capture_output=True)
    for name in ("tdd-path-guard.yml", "tdd-spec.yml"):
        text = (tmp_path / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "pull-requests: read" in text and "GH_TOKEN: ${{ github.token }}" in text, name


# =========================================================================== retirement by record (0.4.1)
RECORD = "legacy: completed in the private development repository"


def _retire_on_main(repo: Path, n: int = 4) -> None:
    """Retire c1 by record in a maintenance PR merged to main (no impl/c1 merge anywhere)."""
    from conftest import commit_all
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "maint/retire-c1")
    assert tdd.main(["--repo", str(repo), "retire", "c1", "--record", RECORD]) == 0
    commit_all(repo, "retire c1 by record")
    merge_to_main(repo, "maint/retire-c1", n)


def test_retired_by_record_without_an_impl_merge(locked: Path) -> None:
    assert not tl.is_retired(locked, "c1")
    _retire_on_main(locked)
    assert tl.is_retired(locked, "c1")
    lock = tl.read_lock(locked, "c1")
    assert lock is not None and lock["status"] == "retired" and lock["retired"]["record"] == RECORD
    assert "c1" not in tl.active_lock_ids(locked)


def test_record_retired_tests_must_pass_in_ci(locked: Path) -> None:
    """Retired by record is graduated: CI demands the tests pass, exactly as after an impl merge."""
    from conftest import commit_all
    _retire_on_main(locked)
    res = tdd.run_ci(locked, "")                         # main, c1 not implemented
    assert not res.ok and any("graduated test failing" in e for e in res.errors), res.errors
    implement(locked)
    commit_all(locked, "implement c1 on main")
    res = tdd.run_ci(locked, "")
    assert res.ok and any("c1: graduated" in s for s in res.summary), (res.errors, res.summary)


def test_record_retired_lock_frees_signature_and_integrity(locked: Path) -> None:
    from conftest import commit_all
    _retire_on_main(locked)
    git(locked, "checkout", "-q", "-b", "impl/c2")
    implement(locked)
    src = locked / "src/pricing/bonds.py"
    src.write_text(src.read_text(encoding="utf-8").replace("years: float)", "years: float, comp: int = 1)"),
                   encoding="utf-8")
    commit_all(locked, "change a formerly locked signature")
    assert tdd.path_guard(locked, "origin/main", "HEAD", "impl/c2").ok
    assert not tdd.integrity(locked, _profile(locked), tl.parse_branch("impl/c2")).problems


def test_retire_is_refused_on_spec_and_impl_branches(locked: Path) -> None:
    assert tl.current_branch(locked) == "impl/c1"
    assert tdd.main(["--repo", str(locked), "retire", "c1", "--record", RECORD]) == 2
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c3")
    assert tdd.main(["--repo", str(locked), "retire", "c1", "--record", RECORD]) == 2
    lock = json.loads((locked / tl.lock_path("c1")).read_text(encoding="utf-8"))
    assert lock["status"] == "active"


def test_retire_needs_a_lock_and_a_record(locked: Path) -> None:
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "maint/x")
    assert tdd.main(["--repo", str(locked), "retire", "nope", "--record", RECORD]) == 1
    assert tdd.main(["--repo", str(locked), "retire", "c1", "--record", "  "]) == 1
