"""The managed hooks (§10), and the settings that install them.

    hooks.py edit       PreToolUse on every file-writing tool: the edit guard (§7.2, §10.2)
    hooks.py stop       Stop: the tdd gate for the session's repo (§9.4, §10.3)
    hooks.py session    SessionStart: logs sessions that start in a tdd repo (§10.4)
        each takes --history-dir, --git and --test-python, written into the installed command
    hooks.py settings --python <runtime> --install-dir <dir> --history-dir <dir> --git <git.exe>
                      --test-python <py312> [--out <file>]
                        the managed-settings.d/tdd.json that runs the three hooks
    hooks.py runtime --from <python prefix> --to <dir>
                        copy a stdlib-only interpreter for the hooks into the admin-only install

Run as `python -I -S hooks.py <hook>`: stdlib only. The edit guard decides from the edited file
alone (its real path, its repo, that repo's branch and pack on origin/main), never from the
session's directory. A denial is PreToolUse JSON with exit 0; a crash exits non-zero, which the
installed command turns into a block when the input names a tdd repo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))              # -I leaves the script's directory off sys.path

import tddlib as tl  # noqa: E402

# Repos whose names make the edit guard start Python and fail closed (§10.2). Adding a repo to the
# scheme means editing this list and reinstalling with admin rights.
TDD_REPOS = ("faultline", "xbrlrag", "macrorag")
FILE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"


# Test seams in tdd.py/tddlib.py that the hooks must never take from their environment: a settings
# `env` block could otherwise point the gate at a fake interpreter or move the event log.
_TEST_SEAMS = ("TDD_PYTHON", "TDD_HISTORY_DIR", "TDD_MANAGED_DIR")


def sanitize_env(history_dir: str | None) -> None:
    """Drop the test seams; the event log location comes only from the installed command line."""
    for name in _TEST_SEAMS:
        os.environ.pop(name, None)
    if history_dir:
        os.environ["TDD_HISTORY_DIR"] = history_dir


# =========================================================================== edit guard
def decide(payload: dict[str, Any], maintenance: bool = False) -> str | None:
    """None to allow, else the reason to deny."""
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return None
    path = os.path.realpath(raw)
    if tl.realpath_under(path, tl.history_root()):
        return "the tdd event log is append-only and written by the hooks"
    top = tl.find_toplevel(path)
    if top is None or not tl.is_tdd_repo(top):
        return None
    top = Path(os.path.realpath(top))
    rel = Path(os.path.relpath(path, top)).as_posix()
    ic = tl.CASE_INSENSITIVE_FS          # a new file under another case is the same file on NTFS
    if ".git" in {p.casefold() if ic else p for p in Path(rel).parts}:
        return "files under .git are changed only by git commands"
    branch = tl.parse_branch(tl.current_branch(top))
    profile = tl.load_profile(top, tl.MAIN, worktree_fallback=True)
    assert profile is not None
    if tl.under(rel, tl.STATE, ic):
        return "the gate's state cache is written by the gate only"
    if tl.under(rel, tl.LOCKS, ic):
        return "lock files are written only by `tdd lock` on a spec branch"
    if tl.glob_match(rel, profile.maintenance_globs(), ic):
        return None if maintenance else \
            "maintenance path: change it outside Claude (or in maintenance mode) through a maintenance PR"
    on_spec = branch.kind == "spec"
    if tl.under(rel, profile.spec_root, ic) or tl.glob_match(rel, tl.load_protected(top), ic):
        if not on_spec:
            return "spec tests and protected fixtures change only on a spec/<id> branch, through a spec PR"
        other = tl.spec_dir_id(rel, profile, ic)
        active = {a.casefold() for a in tl.active_lock_ids(top)} if ic else tl.active_lock_ids(top)
        own = (branch.id or "").casefold() if ic else branch.id
        if other and other != own and other in active:
            return (f"these are the locked tests of {other}, an active change; they change only on "
                    f"spec/{other} (a re-lock)")
        return None
    if tl.under(rel, profile.specs_dir, ic) and branch.kind == "impl":
        return "the spec is fixed during implementation; raise a dispute instead"
    if tl.under(rel, profile.unit_root, ic) and on_spec:
        return "a spec branch changes spec tests and stubs only"
    return None


def edit_hook(payload: dict[str, Any]) -> int:
    reason = decide(payload, maintenance=False)        # maintenance mode arrives in phase 1b
    if reason is None:
        return 0
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    top = tl.find_toplevel(os.path.realpath(path)) if path else None
    project = tl.project_name(top) if top is not None and tl.is_tdd_repo(top) else "_global"
    tl.log_event(project, "edit-denied", tool=payload.get("tool_name"), path=path, reason=reason,
                 session=payload.get("session_id"))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"tdd edit guard: {reason}.",
    }}))
    return 0


# =========================================================================== stop and session
def stop_hook(payload: dict[str, Any]) -> int:
    """Fails open: a crash is a non-blocking hook error, and CI covers the result."""
    top = tl.find_toplevel(payload.get("cwd") or os.getcwd())
    if top is None or not tl.is_tdd_repo(top):
        return 0
    import tdd  # noqa: PLC0415  (only the Stop hook needs the full tools)
    decision = tdd.stop(top, payload)
    if decision:
        print(json.dumps(decision))
    return 0


def session_hook(payload: dict[str, Any]) -> int:
    try:
        top = tl.find_toplevel(payload.get("cwd") or os.getcwd())
        if top is not None and tl.is_tdd_repo(top):
            tl.log_event(tl.project_name(top), "session-start", branch=tl.current_branch(top),
                         session=payload.get("session_id"), source=payload.get("source"),
                         maintenance=os.environ.get("TDD_MAINTENANCE") == "1",
                         entrypoint=os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    except Exception:  # noqa: BLE001
        pass
    return 0


# =========================================================================== managed settings
@dataclass(frozen=True)
class HookArgs:
    """Values written into each installed hook command, never taken from the environment."""
    history_dir: str | None = None
    git: str | None = None              # absolute git.exe
    gh: str | None = None               # absolute gh.exe, for retirement via GitHub (optional)
    test_python: str | None = None      # the project's interpreter, for the controlled test run

    def render(self) -> str:
        pairs = (("--history-dir", self.history_dir), ("--git", self.git), ("--test-python", self.test_python),
                 ("--gh", self.gh))
        return "".join(f' {flag} "{Path(v).as_posix()}"' for flag, v in pairs if v)


def edit_guard_command(python: str, install_dir: str, names: tuple[str, ...] = TDD_REPOS,
                       prefilter: bool = False, history_dir: str | None = None,
                       args: HookArgs | None = None) -> str:
    """The edit guard's hook command (§10.2), bash builtins only: under Git Bash on Windows every
    $(...), pipe or external command costs a process start.

    prefilter=False (installed): Python starts on every edit and decides from the file's real path,
    so junctions and worktrees in folders with any name are covered. A Python failure blocks only
    when the input names a tdd repo. prefilter=True skips Python unless the input names a tdd repo
    (faster, but a junction or an unnamed worktree then isn't guarded). The names are written in
    here, never read from a file at run time: a CRLF names file once made the guard fail open.
    """
    args = args or HookArgs(history_dir=history_dir)
    py, script = Path(python).as_posix(), (Path(install_dir) / "hooks.py").as_posix()
    pattern = "|".join(re.escape(n) for n in names)
    head = f"IFS= read -r -d '' in; shopt -s nocasematch; re='({pattern})'; "
    run = f'"{py}" -I -S "{script}" edit{args.render()} <<<"$in" && exit 0; '
    blocked = '{ echo "tdd edit guard failed; edit to a tdd repo blocked" >&2; exit 2; }'
    if prefilter:
        return head + "[[ $in =~ $re ]] || exit 0; " + run + blocked
    return head + run + f"[[ $in =~ $re ]] && {blocked}; exit 0"


def stop_gate_command(python: str, install_dir: str, history_dir: str | None = None,
                      args: HookArgs | None = None) -> str:
    """Runs the gate only inside a repo with .claude/tdd/; walks up with builtins, not git."""
    args = args or HookArgs(history_dir=history_dir)
    py, script = Path(python).as_posix(), (Path(install_dir) / "hooks.py").as_posix()
    return ('d=$PWD; until [ -e "$d/.git" ]; do [ -n "$d" ] || exit 0; d=${d%/*}; done; '
            f'[ -d "$d/.claude/tdd" ] || exit 0; "{py}" -I -S "{script}" stop{args.render()}')


def session_start_command(python: str, install_dir: str, history_dir: str | None = None,
                          args: HookArgs | None = None) -> str:
    args = args or HookArgs(history_dir=history_dir)
    py, script = Path(python).as_posix(), (Path(install_dir) / "hooks.py").as_posix()
    return f'"{py}" -I -S "{script}" session{args.render()} || true'


def build_settings(python: str, install_dir: str, history_dir: str | None = None, *,
                   git: str | None = None, test_python: str | None = None, gh: str | None = None) -> dict[str, Any]:
    """`python` runs the hooks (the private runtime when installed); `test_python` runs the tests."""
    a = HookArgs(history_dir=history_dir, git=git, gh=gh, test_python=test_python)
    return {"hooks": {
        "PreToolUse": [{"matcher": FILE_TOOLS, "hooks": [
            {"type": "command", "command": edit_guard_command(python, install_dir, args=a),
             "timeout": 30, "statusMessage": "tdd edit guard"}]}],
        "Stop": [{"hooks": [
            {"type": "command", "command": stop_gate_command(python, install_dir, args=a), "timeout": 1800,
             "statusMessage": "tdd Stop gate"}]}],
        "SessionStart": [{"hooks": [
            {"type": "command", "command": session_start_command(python, install_dir, args=a), "timeout": 15}]}],
    }}


# =========================================================================== private runtime
# The hooks' own interpreter: python.exe, its DLLs and the standard library, copied into the
# admin-only install, so the code that runs the guard can't be changed by a non-admin. The hooks use
# only the standard library (-I -S), so site-packages is left out; tests still run on the project's
# interpreter (--test-python).
_SKIP_LIB = {"site-packages", "test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3"}


def copy_runtime(prefix: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in prefix.iterdir():
        if f.is_file() and (f.suffix.lower() == ".dll" or f.name.lower() in ("python.exe", "pythonw.exe")):
            shutil.copy2(f, dest / f.name)
    if (prefix / "DLLs").is_dir():
        shutil.copytree(prefix / "DLLs", dest / "DLLs", dirs_exist_ok=True)
    shutil.copytree(prefix / "Lib", dest / "Lib", dirs_exist_ok=True,
                    ignore=lambda d, names: [n for n in names if Path(d).name == "Lib" and n in _SKIP_LIB])
    probe = subprocess.run([str(dest / "python.exe"), "-I", "-S", "-c",
                            "import ast, json, tomllib, subprocess, hashlib, tempfile, argparse, tokenize; print('ok')"],
                           capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "ok":
        raise RuntimeError(f"private runtime does not start: {probe.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hooks.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("edit", "stop", "session"):
        p = sub.add_parser(name)
        p.add_argument("--history-dir")
        p.add_argument("--git")
        p.add_argument("--gh")
        p.add_argument("--test-python")
    s = sub.add_parser("settings")
    s.add_argument("--python", required=True, help="interpreter that runs the hooks (the private runtime)")
    s.add_argument("--install-dir", required=True)
    s.add_argument("--history-dir", required=True)
    s.add_argument("--git", required=True)
    s.add_argument("--gh", help="gh.exe, optional: lets the gate see merges whose branch was deleted")
    s.add_argument("--test-python", required=True, help="the project's interpreter, for the test run")
    s.add_argument("--out")
    r = sub.add_parser("runtime")
    r.add_argument("--from", dest="src", required=True, help="the interpreter's prefix to copy")
    r.add_argument("--to", dest="dest", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "runtime":
        copy_runtime(Path(a.src), Path(a.dest))
        return 0
    if a.cmd == "settings":
        settings = build_settings(a.python, a.install_dir, a.history_dir, git=a.git, test_python=a.test_python,
                                  gh=a.gh)
        text = json.dumps(settings, indent=2) + "\n"
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(text)
        return 0
    sanitize_env(a.history_dir)
    if a.git:
        tl.GIT = a.git
    if a.gh:
        tl.GH = a.gh
    if a.test_python:
        os.environ["TDD_PYTHON"] = a.test_python
    # Hook input is UTF-8 JSON. Under -I, sys.stdin would decode it with the Windows code page and
    # garble non-ASCII paths, so read the bytes; utf-8-sig also tolerates a byte-order mark.
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    return {"edit": edit_hook, "stop": stop_hook, "session": session_hook}[a.cmd](payload)


if __name__ == "__main__":
    sys.exit(main())
