"""The edit guard (§7.2, §10.2), the installed hook commands run through Git Bash, and
the CI path guard (§7.1, §11)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hooks
import tdd
import tddlib as tl
from conftest import BONDS, IMPL, SKILL, commit_all, git, implement, write



# =========================================================================== edit_guard
SPEC_TEST = "tests/spec/c1/test_zero_price.py"


def _deny(path: str | Path) -> str | None:
    return hooks.decide({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})


def test_non_tdd_repo_is_ignored(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert _deny(other / "tests/spec/x.py") is None
    assert _deny(tmp_path / "no-repo" / "x.py") is None


def test_rules_on_impl_branch(locked: Path) -> None:
    assert "spec tests" in (_deny(locked / SPEC_TEST) or "")
    assert "lock files" in (_deny(locked / ".claude/tdd/locks/c1.json") or "")
    assert "maintenance path" in (_deny(locked / ".claude/tdd/profile.toml") or "")
    assert "maintenance path" in (_deny(locked / "tests/spec/conftest.py") or "")
    assert "maintenance path" in (_deny(locked / ".github/workflows/ci.yml") or "")
    assert "maintenance path" in (_deny(locked / "tools/tdd/gate.py") or "")
    assert "state cache" in (_deny(locked / ".claude/tdd/state/c1.json") or "")
    assert ".git" in (_deny(locked / ".git/refs/remotes/origin/main") or "")
    assert "spec tests" in (_deny(locked / "tests/golden/curve.json") or "")      # protected.txt
    assert "spec is fixed" in (_deny(locked / "docs/specs/c1.md") or "")
    assert _deny(locked / "src/pricing/bonds.py") is None
    assert _deny(locked / "tests/unit/test_new.py") is None
    assert _deny(locked / ".claude/tdd/disputes/c1.md") is None


def test_rules_on_spec_branch(spec_branch: Path) -> None:
    assert _deny(spec_branch / SPEC_TEST) is None
    assert _deny(spec_branch / "tests/golden/curve.json") is None
    assert _deny(spec_branch / "docs/specs/c1.md") is None
    assert "lock files" in (_deny(spec_branch / ".claude/tdd/locks/c1.json") or "")
    assert "maintenance path" in (_deny(spec_branch / "tests/spec/conftest.py") or "")
    assert "spec branch" in (_deny(spec_branch / "tests/unit/test_x.py") or "")


def test_other_branch_denies_protected(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "feature/x")
    assert _deny(project / SPEC_TEST) is not None


def test_event_log_is_protected(project: Path, tmp_path: Path) -> None:
    assert "event log" in (_deny(tmp_path / "tdd-history" / "faultline" / "events.jsonl") or "")


def test_path_forms(locked: Path, tmp_path: Path) -> None:
    """Check 9: `..` paths, mixed separators, and a session started elsewhere."""
    elsewhere = tmp_path / "ClaudeConfig"
    elsewhere.mkdir()
    dotdot = f"{elsewhere}{os.sep}..{os.sep}faultline{os.sep}tests{os.sep}spec{os.sep}c1{os.sep}test_zero_price.py"
    assert _deny(dotdot) is not None
    mixed = str(locked / "tests") + "/spec\\c1/test_zero_price.py"
    if os.name == "nt":                          # a backslash is a filename character elsewhere
        assert _deny(mixed) is not None
    upper = str(locked / SPEC_TEST).upper() if os.name == "nt" else str(locked / SPEC_TEST)
    assert _deny(upper) is not None


@pytest.mark.parametrize("name", ["faultline-wt", "tmp"])
def test_worktrees(locked: Path, tmp_path: Path, name: str) -> None:
    """Check 9: a sibling worktree, whether or not its path contains the repo name."""
    wt = tmp_path / name
    git(locked, "worktree", "add", "-q", "-b", f"impl/c1--part{len(name)}", str(wt), "impl/c1")
    assert _deny(wt / SPEC_TEST) is not None
    assert _deny(wt / "src/pricing/bonds.py") is None


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junction(locked: Path, tmp_path: Path) -> None:
    link = tmp_path / "jn"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(locked)], check=True, capture_output=True)
    assert _deny(link / SPEC_TEST) is not None


# --------------------------------------------------------------------------- the hook command
def _bash() -> str | None:
    """Git Bash, never WSL's C:\\Windows\\system32\\bash.exe (it can't see Windows paths)."""
    git_exe = shutil.which("git")
    cand = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", ""),
            str(Path(git_exe).resolve().parent.parent / "bin" / "bash.exe") if git_exe else "",
            r"C:\Program Files\Git\bin\bash.exe"]
    if os.name != "nt":
        cand.append(shutil.which("bash") or "")
    return next((c for c in cand if c and Path(c).exists()), None)


@pytest.mark.skipif(_bash() is None, reason="needs Git Bash")
@pytest.mark.parametrize("prefilter", [True, False])
def test_hook_command_fail_closed_only_for_tdd_names(locked: Path, tmp_path: Path, prefilter: bool) -> None:
    """Check 6: a broken interpreter blocks edits naming a tdd repo and nothing else.
    Uses the exact command the installer writes."""
    scripts = str(SKILL / "scripts")
    bash = _bash()
    assert bash

    def run(python: str, path: Path) -> subprocess.CompletedProcess[str]:
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
        command = hooks.edit_guard_command(python, scripts, prefilter=prefilter, history_dir=str(tmp_path / "h"))
        return subprocess.run([bash, "-c", command], input=payload,
                              capture_output=True, text=True,
                              env={**os.environ, "TDD_HISTORY_DIR": str(tmp_path / "h")})

    broken = "/nonexistent/python.exe"
    assert run(broken, locked / SPEC_TEST).returncode == 2
    assert run(broken, tmp_path / "elsewhere" / "x.py").returncode == 0

    py = Path(sys.executable).as_posix()
    if not prefilter:
        bad_json = subprocess.run([bash, "-c", hooks.edit_guard_command(py, scripts, prefilter=False, history_dir=str(tmp_path / "h"))],
                                  input="{not json", capture_output=True, text=True)
        assert bad_json.returncode == 0                      # crash, but no tdd name in the input
    ok = run(py, locked / SPEC_TEST)
    assert ok.returncode == 0 and json.loads(ok.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = run(py, locked / "src/pricing/bonds.py")
    assert allowed.returncode == 0 and allowed.stdout.strip() == ""


# =========================================================================== hook_commands
def _run(command: str, cwd: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_bash() or "bash", "-c", command], cwd=cwd, input=json.dumps(payload),
                          capture_output=True, text=True)


def test_stop_command_skips_non_tdd_dirs(tmp_path: Path) -> None:
    cmd = hooks.stop_gate_command("/nonexistent/python.exe", str(SKILL / "scripts"))
    plain = tmp_path / "plain"
    (plain / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    assert _run(cmd, plain / "sub", {}).returncode == 0      # git repo without .claude/tdd
    assert _run(cmd, tmp_path, {}).returncode == 0           # not in a repo at all


def test_stop_command_gates_tdd_repo(locked: Path) -> None:
    cmd = hooks.stop_gate_command(sys.executable, str(SKILL / "scripts"), str(locked.parent / "h"))
    r = _run(cmd, locked / "src", {"cwd": str(locked / "src")})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["decision"] == "block"


def test_settings_shape() -> None:
    s = hooks.build_settings("C:/py/python.exe", "C:/Program Files/ClaudeCode/tdd")
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert {"PreToolUse", "Stop", "SessionStart"} == set(s["hooks"])


def test_names_are_inline_in_the_command() -> None:
    """No names file is read at run time (a CRLF names file once made the guard fail open)."""
    cmd = hooks.edit_guard_command("C:/py/python.exe", "C:/tdd")
    assert "re='(faultline|xbrlrag|macrorag)'" in cmd and ".txt" not in cmd


# =========================================================================== path_guard
def test_spec_pr_is_clean(spec_branch: Path) -> None:
    tdd.write_lock(spec_branch, tdd.build_lock(spec_branch, tl.load_profile(spec_branch), "c1"))  # type: ignore[arg-type]
    commit_all(spec_branch, "test(spec): c1")
    res = tdd.path_guard(spec_branch, "origin/main", "HEAD", "spec/c1")
    assert res.ok and res.kind == "spec", res.errors
    assert res.files["src/pricing/bonds.py"] == "src"


def test_spec_pr_with_real_code_fails(spec_branch: Path) -> None:
    (spec_branch / "src/pricing/bonds.py").write_text(BONDS + IMPL, encoding="utf-8")
    commit_all(spec_branch, "sneaky")
    res = tdd.path_guard(spec_branch, "origin/main", "HEAD")
    assert not res.ok and any("not a stub" in e for e in res.errors)


def test_mixed_spec_and_impl_fails(spec_branch: Path) -> None:
    write(spec_branch, "README.md", "hello\n")
    commit_all(spec_branch, "mixed")
    res = tdd.path_guard(spec_branch, "origin/main", "HEAD")
    assert not res.ok and any("mixed PR" in e for e in res.errors)


def test_maintenance_pr(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "maint/x")
    write(project, ".github/workflows/x.yml", "on: push\n")
    commit_all(project, "workflow")
    res = tdd.path_guard(project, "origin/main", "HEAD")
    assert res.ok and res.kind == "maintenance" and res.notes
    write(project, "src/pricing/x.py", "X = 1\n")
    commit_all(project, "plus code")
    assert not tdd.path_guard(project, "origin/main", "HEAD").ok


def test_impl_pr_may_not_change_locked_signature(locked: Path) -> None:
    implement(locked)
    commit_all(locked, "impl")
    assert tdd.path_guard(locked, "origin/main", "HEAD", "impl/c1").ok
    src = locked / "src/pricing/bonds.py"
    src.write_text(src.read_text(encoding="utf-8").replace("years: float)", "years: float, comp: int = 1)"),
                   encoding="utf-8")
    commit_all(locked, "change signature")
    res = tdd.path_guard(locked, "origin/main", "HEAD", "impl/c1")
    assert not res.ok and any("locked signature changed" in e for e in res.errors)


def test_impl_pr_may_not_touch_spec_tests(locked: Path) -> None:
    implement(locked)
    write(locked, "tests/spec/c1/test_more.py", "def test_x():\n    pass\n")
    commit_all(locked, "impl and a test")
    res = tdd.path_guard(locked, "origin/main", "HEAD", "impl/c1")
    assert not res.ok


# =========================================================================== review fixes
def test_settings_files_are_maintenance_paths(locked: Path) -> None:
    """P2: .claude/settings*.json can set env vars for the hooks, so agents can't edit them."""
    assert "maintenance path" in (_deny(locked / ".claude/settings.json") or "")
    assert "maintenance path" in (_deny(locked / ".claude/settings.local.json") or "")


def test_hooks_ignore_test_env_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P2: TDD_PYTHON / TDD_HISTORY_DIR / TDD_MANAGED_DIR from the environment are dropped by the hooks;
    the history dir comes only from the installed command line."""
    monkeypatch.setenv("TDD_PYTHON", "C:/evil/python.exe")
    monkeypatch.setenv("TDD_HISTORY_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("TDD_MANAGED_DIR", str(tmp_path / "fake"))
    hooks.sanitize_env(str(tmp_path / "pinned"))
    assert "TDD_PYTHON" not in os.environ and "TDD_MANAGED_DIR" not in os.environ
    assert os.environ["TDD_HISTORY_DIR"] == str(tmp_path / "pinned")
    hooks.sanitize_env(None)
    assert "TDD_HISTORY_DIR" not in os.environ
    cmd = hooks.edit_guard_command("C:/py/python.exe", "C:/tdd", history_dir="D:/cfg/tdd-history")
    assert '--history-dir "D:/cfg/tdd-history"' in cmd


def test_path_guard_trusts_main_not_a_stale_merge_base(project: Path) -> None:
    """A PR whose merge-base predates the tdd pack is still classified with main's profile."""
    git(project, "checkout", "-q", "--orphan", "stale")
    git(project, "rm", "-rq", "--cached", ".")
    for p in list(project.iterdir()):
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    write(project, "README.md", "old\n")
    commit_all(project, "pre-pack history")
    base = git(project, "rev-parse", "HEAD").strip()
    write(project, "tools/tdd/tdd.py", "# replaced\n")
    write(project, "src/x.py", "X = 1\n")
    commit_all(project, "edit tools and code")
    res = tdd.path_guard(project, base, "HEAD")
    assert res.kind != "bootstrap" and not res.ok
    assert any("mixed PR" in e for e in res.errors), res.errors


# =========================================================================== review fixes
def _installed_run(command: str, path: Path) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
    return subprocess.run([_bash() or "bash", "-c", command], input=payload, capture_output=True, text=True)


def _denied(r: subprocess.CompletedProcess[str]) -> bool:
    return r.returncode == 0 and '"deny"' in r.stdout


@pytest.mark.skipif(_bash() is None, reason="needs Git Bash")
def test_installed_command_follows_junctions_and_unnamed_worktrees(locked: Path, tmp_path: Path) -> None:
    """The installed command must reach the real-path decision even when the given path has no
    repo name in it (a junction, or a worktree in a folder with another name)."""
    cmd = hooks.build_settings(sys.executable, str(SKILL / "scripts"), str(tmp_path / "h"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    wt = tmp_path / "wt-cost"
    git(locked, "worktree", "add", "-q", "-b", "impl/c1--part9", str(wt), "impl/c1")
    assert _denied(_installed_run(cmd, wt / SPEC_TEST))
    if os.name == "nt":
        link = tmp_path / "jn"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(locked / "tests" / "spec")], check=True,
                       capture_output=True)
        assert _denied(_installed_run(cmd, link / "c1" / "test_zero_price.py"))


def test_spec_branch_may_not_edit_another_changes_locked_tests(locked: Path) -> None:
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c2")
    assert "locked tests of c1" in (_deny(locked / SPEC_TEST) or "")
    assert _deny(locked / "tests/spec/c2/test_new.py") is None


def test_path_guard_flags_locked_tests_changed_without_relock(locked: Path) -> None:
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c2")
    p = locked / SPEC_TEST
    p.write_text(p.read_text(encoding="utf-8").replace("rel=1e-12", "rel=1e-3"), encoding="utf-8")
    commit_all(locked, "loosen c1 from spec/c2")
    res = tdd.path_guard(locked, "origin/main", "HEAD", "spec/c2")
    assert not res.ok and any("c1" in e and "without re-locking" in e for e in res.errors), res.errors


def test_hooks_take_git_and_test_python_from_the_command_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import io
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    monkeypatch.setenv("TDD_PYTHON", "C:/evil/python.exe")
    old_git = tl.GIT
    try:
        hooks.main(["session", "--history-dir", str(tmp_path / "h"), "--git", "C:/trusted/git.exe",
                    "--test-python", "C:/py312/python.exe"])
        assert tl.GIT == "C:/trusted/git.exe" and os.environ["TDD_PYTHON"] == "C:/py312/python.exe"
    finally:
        tl.GIT = old_git
    s = hooks.build_settings("C:/rt/python.exe", "C:/tdd", "D:/h", git="C:/git.exe", test_python="D:/py/python.exe")
    for hook in ("PreToolUse", "Stop", "SessionStart"):
        c = s["hooks"][hook][0]["hooks"][0]["command"]
        assert '--git "C:/git.exe"' in c and '--test-python "D:/py/python.exe"' in c, hook


@pytest.mark.skipif(os.name != "nt", reason="the private runtime is built on Windows")
def test_private_runtime_runs_the_hooks(tmp_path: Path) -> None:
    """The hooks' own interpreter: a stdlib-only copy that can live in the admin-only install."""
    dest = tmp_path / "python"
    hooks.copy_runtime(Path(sys.executable).parent, dest)
    assert not (dest / "Lib" / "site-packages").exists()
    r = subprocess.run([str(dest / "python.exe"), "-I", "-S", str(SKILL / "scripts" / "hooks.py"), "edit",
                        "--history-dir", str(tmp_path / "h")],
                       input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "x.py")}}),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# =========================================================================== review fixes
def test_path_guard_handles_non_ascii_and_spaces(project: Path) -> None:
    """git quotes non-ASCII paths by default; the guard must classify the real names."""
    git(project, "checkout", "-q", "-b", "feature/x")
    write(project, ".github/workflows/été.yml", "on: push\n")
    write(project, "src/pricing/my module.py", "X = 1\n")
    commit_all(project, "workflow with accents, module with a space")
    res = tdd.path_guard(project, "origin/main", "HEAD")
    assert res.files.get(".github/workflows/été.yml") == "maintenance", res.files
    assert "src/pricing/my module.py" in res.files
    assert not res.ok and any("mixed PR" in e for e in res.errors)


def test_files_directly_under_spec_root_are_maintenance_paths(locked: Path) -> None:
    """Spec tests can import modules beside them in <spec_root>/, so those are protected too."""
    assert "maintenance path" in (_deny(locked / "tests/spec/helpers.py") or "")
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c2")
    assert "maintenance path" in (_deny(locked / "tests/spec/helpers.py") or "")


def test_hook_reads_utf8_input_with_non_ascii_path_and_bom(locked: Path, tmp_path: Path) -> None:
    """Hook input is UTF-8; under -I the hook must not decode it with the Windows code page."""
    repo = locked.parent / "faultline-été"
    shutil.copytree(locked, repo)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(repo / SPEC_TEST)}},
                         ensure_ascii=False).encode("utf-8")
    for data in (payload, b"\xef\xbb\xbf" + payload):
        r = subprocess.run([sys.executable, "-I", "-S", str(SKILL / "scripts" / "hooks.py"), "edit",
                            "--history-dir", str(tmp_path / "h")], input=data, capture_output=True)
        assert r.returncode == 0 and b'"deny"' in r.stdout, r.stderr.decode(errors="replace")


# =========================================================================== deep review fixes
@pytest.mark.skipif(os.name != "nt", reason="NTFS is case-insensitive")
def test_edit_guard_matches_protected_paths_case_insensitively(locked: Path) -> None:
    """realpath only fixes the case of components that exist; a new file under another case must
    still be recognised, because NTFS resolves it to the protected path."""
    assert "maintenance path" in (_deny(locked / ".CLAUDE" / "Settings.Local.JSON") or "")
    assert "maintenance path" in (_deny(locked / ".GitHub" / "Workflows" / "x.yml") or "")
    assert "lock files" in (_deny(locked / ".claude" / "TDD" / "Locks" / "c9.json") or "")
    assert "spec tests" in (_deny(locked / "Tests" / "Spec" / "c1" / "test_new.py") or "")


def test_path_guard_treats_case_variants_of_maintenance_paths_as_maintenance(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "feature/x")
    write(project, ".CLAUDE/Settings.json", '{"env": {}}\n')
    write(project, "src/pricing/x.py", "X = 1\n")
    commit_all(project, "case variant of a settings file, plus code")
    res = tdd.path_guard(project, "origin/main", "HEAD")
    assert [c for f, c in res.files.items() if f.casefold() == ".claude/settings.json"] == ["maintenance"], res.files
    assert not res.ok and any("mixed PR" in e for e in res.errors)


def test_settings_carry_gh_when_given() -> None:
    s = hooks.build_settings("C:/rt/python.exe", "C:/tdd", "D:/h", git="C:/git.exe", test_python="D:/py.exe",
                             gh="C:/gh.exe")
    for hook in ("PreToolUse", "Stop", "SessionStart"):
        c = s["hooks"][hook][0]["hooks"][0]["command"]
        assert '--gh "C:/gh.exe"' in c and '--test-python "D:/py.exe"' in c, hook
    assert "--gh" not in hooks.build_settings("C:/rt/python.exe", "C:/tdd", "D:/h", git="C:/git.exe",
                                              test_python="D:/py.exe")["hooks"]["Stop"][0]["hooks"][0]["command"]


# =========================================================================== lock status changes (0.4.1)
def _set_status(repo: Path, cid: str, status: str) -> None:
    p = repo / tl.lock_path(cid)
    lock = json.loads(p.read_text(encoding="utf-8"))
    lock["status"] = status
    p.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_retiring_a_lock_is_a_maintenance_change(locked: Path) -> None:
    """A spec PR can't release another change's (or its own) locked tests by flipping the status."""
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c9")
    _set_status(locked, "c1", "retired")
    commit_all(locked, "retire c1 from a spec branch")
    res = tdd.path_guard(locked, "origin/main", "HEAD", "spec/c9")
    assert res.kind == "maintenance", (res.kind, res.errors)
    p = locked / SPEC_TEST
    p.write_text(p.read_text(encoding="utf-8").replace("rel=1e-12", "rel=1e-3"), encoding="utf-8")
    commit_all(locked, "and loosen its test")
    res = tdd.path_guard(locked, "origin/main", "HEAD", "spec/c9")
    assert not res.ok and any("mixed PR" in e for e in res.errors), res.errors


def test_any_status_leaving_active_is_a_maintenance_change(locked: Path) -> None:
    git(locked, "checkout", "-q", "main")
    git(locked, "checkout", "-q", "-b", "spec/c9")
    _set_status(locked, "c1", "on-hold")
    commit_all(locked, "park c1")
    assert tdd.path_guard(locked, "origin/main", "HEAD", "spec/c9").kind == "maintenance"
