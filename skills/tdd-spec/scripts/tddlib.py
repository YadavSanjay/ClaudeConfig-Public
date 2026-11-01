"""tdd-spec core library: repo discovery, git, profile, locks, state, event log, AST signatures
and stubs, and the outcome classifier. Design: ../design/plan.md.

Stdlib only: the managed hooks import this under `python -I -S` (no site-packages). Paths inside a
repo are always repo-relative POSIX strings.
"""

from __future__ import annotations

import ast
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# =========================================================================== tddlib
VERSION = "0.4.1"

PACK = ".claude/tdd"
LOCKS = f"{PACK}/locks"
STATE = f"{PACK}/state"
PROFILE = f"{PACK}/profile.toml"
PROTECTED = f"{PACK}/protected.txt"
MAIN = "origin/main"
GIT = "git"            # the hooks set an absolute path from their installed command line (--git)
VARIANTS = ("zero", "nan", "none", "empty", "zero_instance")   # red_check pass 2, see tdd_plugin

ID_RX = r"[a-z0-9](?:[a-z0-9._]|-(?!-))*"          # change id; "--" is reserved for --part<N>
BRANCH_RX = re.compile(rf"^(spec|impl)/({ID_RX})(?:--part(\d+))?$")
MERGE_RX = r"^Merge pull request #\d+ from [^/\s]+/impl/{id}$"


# =========================================================================== branches
@dataclass(frozen=True)
class Branch:
    name: str | None
    kind: str            # "spec" | "impl" | "other"
    id: str | None = None
    part: int | None = None


def parse_branch(name: str | None) -> Branch:
    m = BRANCH_RX.match(name or "")
    if not m or (m.group(1) == "spec" and m.group(3)):
        return Branch(name, "other")
    return Branch(name, m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None)


# =========================================================================== repo discovery
def find_toplevel(path: str | Path) -> Path | None:
    """Walk up from `path` (which may not exist yet) to the directory holding `.git` (dir or file)."""
    p = Path(os.path.realpath(path))
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return None


def git_dir(top: Path) -> Path:
    dot = top / ".git"
    if dot.is_dir():
        return dot
    text = dot.read_text(encoding="utf-8").strip()           # worktree: "gitdir: <path>"
    gd = Path(text.split(":", 1)[1].strip())
    return gd if gd.is_absolute() else (top / gd).resolve()


def current_branch(top: Path) -> str | None:
    """Read HEAD directly (no subprocess). None when detached."""
    head = (git_dir(top) / "HEAD").read_text(encoding="utf-8").strip()
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None


def is_tdd_repo(top: Path | None) -> bool:
    return top is not None and (top / PACK).is_dir()


def project_name(top: Path) -> str:
    """Name for the event log: the main checkout's folder, so worktrees log under their repo."""
    gd = git_dir(top)
    common = gd.parent.parent if gd.parent.name == "worktrees" else gd
    return (common.parent if common.name == ".git" else top).name.lower()


# =========================================================================== git
def git(top: Path, *args: str, check: bool = True, timeout: float = 60) -> str:
    r = subprocess.run([GIT, "-c", "core.quotepath=off", "-C", str(top), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def git_ok(top: Path, *args: str) -> bool:
    return subprocess.run([GIT, "-C", str(top), *args], capture_output=True).returncode == 0


def show(top: Path, ref: str, path: str) -> str | None:
    r = subprocess.run([GIT, "-C", str(top), "show", f"{ref}:{path}"], capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else None


def has_ref(top: Path, ref: str) -> bool:
    return git_ok(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def fetch_main(top: Path, timeout: float = 10) -> tuple[bool, str]:
    """Fetch origin/main. Returns (fetched, note) where note gives the ref's age when it failed."""
    try:
        r = subprocess.run([GIT, "-C", str(top), "fetch", "--quiet", "origin", "main"],
                           capture_output=True, timeout=timeout)
        if r.returncode == 0:
            return True, ""
    except subprocess.TimeoutExpired:
        pass
    return False, f"could not fetch origin/main; using the local ref ({ref_age(top)})"


def ref_age(top: Path, ref: str = MAIN) -> str:
    log = git(top, "reflog", "-1", "--format=%cr", ref, check=False).strip()
    return f"last updated {log}" if log else "age unknown"


def nul_split(out: str) -> list[str]:
    """Paths from a `-z` git listing: exact names, including spaces and non-ASCII characters."""
    return [p for p in out.split("\0") if p]


def ls_tree(top: Path, ref: str, path: str = "") -> list[str]:
    return nul_split(git(top, "ls-tree", "-r", "-z", "--name-only", ref, *([path] if path else []), check=False))


def is_ancestor(top: Path, commit: str, of: str = "HEAD") -> bool:
    return git_ok(top, "merge-base", "--is-ancestor", commit, of)


# =========================================================================== globs
def _glob_rx(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        else:
            c = pattern[i]
            out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
            i += 1
    return re.compile(out + r"\Z")


# Windows file systems are case-insensitive, and realpath fixes the case only of components that
# already exist: a new `.CLAUDE/Settings.json` is the protected `.claude/settings.json` on disk.
# Protection checks therefore compare case-insensitively (ignore_case=True).
CASE_INSENSITIVE_FS = os.name == "nt"


def glob_match(path: str, patterns: list[str] | str, ignore_case: bool = False) -> bool:
    pats = [patterns] if isinstance(patterns, str) else patterns
    if ignore_case:
        path, pats = path.casefold(), [p.casefold() for p in pats]
    return any(_glob_rx(p).match(path) for p in pats if p)


def under(path: str, root: str, ignore_case: bool = False) -> bool:
    root = root.rstrip("/")
    if ignore_case:
        path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root + "/")


# =========================================================================== profile
@dataclass
class Profile:
    spec_root: str = "tests/spec"
    unit_root: str = "tests/unit"
    dispute_dir: str = f"{PACK}/disputes"
    specs_dir: str = "docs/specs"
    src_roots: list[str] = field(default_factory=lambda: ["src"])
    pythonpath: list[str] = field(default_factory=lambda: ["src", "."])
    plugins: list[str] = field(default_factory=list)
    budget: int = 8
    valuation_date: str = ""
    gate_profile: str = "tdd-gate"
    verify_profile: str = "tdd-verify"
    source: str = "default"

    @property
    def uses_hypothesis(self) -> bool:
        return any("hypothesis" in p for p in self.plugins)

    def maintenance_globs(self) -> list[str]:
        # .claude/settings*.json: an `env` block there reaches the hooks' environment.
        return [PROFILE, PROTECTED, *self.spec_maintenance_globs(), "tools/tdd/**",
                ".github/workflows/**", ".githooks/**", ".claude/settings.json", ".claude/settings.local.json"]

    def spec_maintenance_globs(self) -> list[str]:
        """Maintenance paths the gate's own test run depends on: every file directly in the spec root
        (conftest.py, and any module there, since the spec root is on the test sys.path) and _shared/."""
        sr = self.spec_root.rstrip("/")
        return [f"{sr}/*", f"{sr}/_shared/**"]


_PROFILE_KEYS = {"spec_root", "unit_root", "dispute_dir", "specs_dir", "src_roots", "pythonpath",
                 "plugins", "budget", "valuation_date"}


def parse_profile(text: str, source: str) -> Profile:
    data = tomllib.loads(text)
    hyp = data.pop("hypothesis", {}) or {}
    unknown = set(data) - _PROFILE_KEYS
    if unknown:
        raise ValueError(f"profile.toml: unknown keys {sorted(unknown)}")
    prof = Profile(**data, source=source)
    prof.gate_profile = hyp.get("gate_profile", prof.gate_profile)
    prof.verify_profile = hyp.get("verify_profile", prof.verify_profile)
    return prof


def load_profile(top: Path, ref: str | None = MAIN, worktree_fallback: bool = False) -> Profile | None:
    """The trusted profile comes from `ref`. Fallbacks are only for path classification."""
    if ref:
        text = show(top, ref, PROFILE)
        if text is not None:
            return parse_profile(text, ref)
    if worktree_fallback:
        p = top / PROFILE
        return parse_profile(p.read_text(encoding="utf-8"), "worktree") if p.exists() else Profile()
    return None


def load_protected(top: Path, ref: str | None = MAIN) -> list[str]:
    text = show(top, ref, PROTECTED) if ref else None
    if text is None:
        p = top / PROTECTED
        text = p.read_text(encoding="utf-8") if p.exists() else ""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# =========================================================================== locks
def lock_path(cid: str) -> str:
    return f"{LOCKS}/{cid}.json"


def lock_ids(top: Path, ref: str = MAIN) -> list[str]:
    return sorted(Path(p).stem for p in ls_tree(top, ref, LOCKS) if p.endswith(".json"))


def read_lock(top: Path, cid: str, ref: str = MAIN) -> dict[str, Any] | None:
    text = show(top, ref, lock_path(cid))
    return json.loads(text) if text else None


def lock_versions(top: Path, cid: str, ref: str = MAIN) -> list[str]:
    """Commits on `ref` that changed the lock, newest first."""
    out = git(top, "log", "--format=%H", ref, "--", lock_path(cid), check=False)
    return [c for c in out.split() if c]


def applicable_lock(top: Path, cid: str) -> tuple[str, str | None]:
    """How a lock on origin/main relates to HEAD: ("current"|"stale"|"absent", commit)."""
    versions = lock_versions(top, cid)
    for i, c in enumerate(versions):
        if is_ancestor(top, c):
            return ("current" if i == 0 else "stale"), c
    return "absent", None


def active_lock_ids(top: Path, ref: str = MAIN) -> set[str]:
    """Changes whose lock on `ref` is active and not yet retired."""
    out = set()
    for cid in lock_ids(top, ref):
        lock = read_lock(top, cid, ref)
        if lock and lock.get("status") == "active" and not is_retired(top, cid, ref):
            out.add(cid)
    return out


def spec_dir_id(rel: str, profile: Profile, ignore_case: bool = False) -> str | None:
    """The change id of a path under <spec_root>/<id>/, else None."""
    root = profile.spec_root.strip("/") + "/"
    if ignore_case:
        rel, root = rel.casefold(), root.casefold()
    if not rel.startswith(root):
        return None
    parts = rel[len(root):].split("/", 1)
    return parts[0] if len(parts) == 2 else None


# =========================================================================== retirement
# A lock retires when the PR from exactly impl/<id> is merged into main, or by record: a lock on `ref`
# whose status is "retired" (written by `tdd retire`, merged only through a maintenance PR — the path
# guard classifies any status leaving "active" as maintenance). The record covers history that is not
# in this repository, e.g. a mirror published without its development history. A merge is proven by
# any one of three signals; none depends on the merge title alone:
#   1. a merge commit on `ref` with GitHub's default subject "Merge pull request #N from o/impl/<id>";
#   2. a merge commit on `ref` whose second parent is the tip of origin/impl/<id> (custom title,
#      branch not yet pruned);
#   3. GitHub's record of a merged PR whose head ref is exactly impl/<id>, with its merge commit on
#      `ref` (custom title, branch deleted). Needs `gh`; any failure counts as "not known".
# A positive result is cached in the state folder and re-checked against `ref` before use; a negative
# GitHub answer is cached for NEGATIVE_TTL seconds. Being wrong in the "not retired" direction is
# safe: the lock just stays active.
GH = "gh"              # the hooks set an absolute path from their installed command line (--gh)
NEGATIVE_TTL = 600
_RETIRED_KEY = "_retired"
RETIRED = "retired"    # lock status of a lock retired by record
_memo: dict[tuple[str, str, str], bool] = {}


def github_repo_from_url(url: str) -> tuple[str, str] | None:
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$",
                 url.strip())
    return (m.group(1), m.group(2)) if m else None


def github_pulls(top: Path, branch: str) -> list[dict[str, Any]] | None:
    """Closed PRs whose head is `branch` in origin's GitHub repo, or None when unknown."""
    repo = github_repo_from_url(git(top, "remote", "get-url", "origin", check=False))
    if repo is None:
        return None
    owner, name = repo
    try:
        r = subprocess.run([GH, "api", "-X", "GET", f"repos/{owner}/{name}/pulls", "-f", "state=closed",
                            "-f", f"head={owner}:{branch}", "-f", "per_page=100"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def clear_retirement_cache(top: Path) -> None:
    _memo.clear()
    p = state_file(top, _RETIRED_KEY)
    if p.exists():
        p.unlink()


def _merges(top: Path, ref: str) -> list[tuple[str, list[str], str]]:
    """(sha, parents, subject) of merge commits on `ref`'s first-parent history."""
    out = git(top, "log", "--first-parent", "--merges", "--format=%H%x00%P%x00%s", ref, check=False)
    rows = []
    for line in out.splitlines():
        sha, parents, subject = (line.split("\0") + ["", ""])[:3]
        rows.append((sha, parents.split(), subject))
    return rows


def _retired_uncached(top: Path, cid: str, ref: str) -> tuple[bool, str | None]:
    branch = f"impl/{cid}"
    merges = _merges(top, ref)
    rx = re.compile(MERGE_RX.format(id=re.escape(cid)))
    for sha, _parents, subject in merges:                      # 1. default subject
        if rx.match(subject):
            return True, sha
    tip = git(top, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}",
              check=False).strip()
    if tip:                                                     # 2. second parent is the branch tip
        for sha, parents, _subject in merges:
            if len(parents) > 1 and tip in parents[1:]:
                return True, sha
    cache = load_state(top, _RETIRED_KEY)
    neg = cache.get("negative", {}).get(cid)
    if neg and dt.datetime.now(dt.timezone.utc).timestamp() - float(neg) < NEGATIVE_TTL:
        return False, None
    pulls = github_pulls(top, branch)                          # 3. GitHub's record
    if pulls is None:
        return False, None
    for pr in pulls:
        sha = pr.get("merge_commit_sha") or ""
        if (pr.get("head") or {}).get("ref") == branch and pr.get("merged_at") and sha \
                and is_ancestor(top, sha, ref):
            return True, sha
    cache.setdefault("negative", {})[cid] = dt.datetime.now(dt.timezone.utc).timestamp()
    try:
        save_state(top, _RETIRED_KEY, cache)
    except OSError:
        pass
    return False, None


def is_retired(top: Path, cid: str, ref: str = MAIN) -> bool:
    key = (str(top), cid, ref)
    if _memo.get(key):                       # only positives are memoised: a merge can't be undone
        return True
    lock = read_lock(top, cid, ref)
    if lock and lock.get("status") == RETIRED:   # retired by record, read from `ref` (trusted main)
        _memo[key] = True
        return True
    cache = load_state(top, _RETIRED_KEY)
    known = cache.get("positive", {}).get(cid)
    if known and is_ancestor(top, known, ref):
        _memo[key] = True
        return True
    retired, sha = _retired_uncached(top, cid, ref)
    if retired and sha:
        cache = load_state(top, _RETIRED_KEY)
        cache.setdefault("positive", {})[cid] = sha
        try:
            save_state(top, _RETIRED_KEY, cache)
        except OSError:
            pass
        _memo[key] = True
    return retired


# =========================================================================== files
def sha256_bytes(data: bytes) -> str:
    """Line endings normalised, so a lock made on Windows (autocrlf) verifies on Linux CI."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def show_bytes(top: Path, ref: str, path: str) -> bytes | None:
    r = subprocess.run([GIT, "-C", str(top), "show", f"{ref}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def tree_files(top: Path, rel_dir: str) -> dict[str, str]:
    """{repo-relative path: sha256} for every file under rel_dir, skipping caches."""
    root = top / rel_dir
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith((".pyc", ".pyo")):
            out[p.relative_to(top).as_posix()] = sha256_file(p)
    return out


def worktree_glob(top: Path, patterns: list[str], include_ignored: bool = False) -> list[str]:
    """Tracked and untracked files matching `patterns`. Asks git, limited by the patterns as
    pathspecs, instead of walking the tree, so unrelated folders (data/, node_modules/) cost nothing.

    include_ignored=True also lists gitignored files. Integrity checks use it: ignoring a file
    (.gitignore, .git/info/exclude) must not hide it from the comparison with main.
    """
    if not patterns:
        return []
    args = ["ls-files", "-z", "--cached", "--others", *([] if include_ignored else ["--exclude-standard"]),
            "--", *(f":(glob){p}" for p in patterns)]
    listed = nul_split(git(top, *args, check=False))
    return sorted({rel for rel in listed if glob_match(rel, patterns) and (top / rel).is_file()})


# =========================================================================== state cache
def state_file(top: Path, key: str) -> Path:
    return top / STATE / f"{key}.json"


def load_state(top: Path, key: str) -> dict[str, Any]:
    p = state_file(top, key)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(top: Path, key: str, data: dict[str, Any]) -> None:
    p = state_file(top, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# =========================================================================== event log
def history_root() -> Path:
    if env := os.environ.get("TDD_HISTORY_DIR"):
        return Path(env)
    if cfg := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(cfg) / "tdd-history"
    return Path.home() / ".claude" / "tdd-history"


def log_event(project: str, etype: str, **fields: Any) -> None:
    """Append one event. Never raises: logging must not change a hook's decision."""
    try:
        root = history_root() / project
        root.mkdir(parents=True, exist_ok=True)
        rec = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "type": etype, "tool_version": VERSION, **fields}
        with (root / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        pass


def realpath_under(path: str | Path, root: str | Path) -> bool:
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def fnmatch_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# =========================================================================== signatures
Func = ast.FunctionDef | ast.AsyncFunctionDef
TDD_PREFIX = "tdd:"


def iter_functions(tree: ast.Module) -> Iterator[tuple[str, Func]]:
    """(qualname, node) for module functions and methods of (nested) classes; not inner functions."""
    def walk(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, Func]]:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, f"{prefix}{node.name}.")
    yield from walk(tree.body, "")


def _tdd_raise_id(node: ast.stmt) -> str | None:
    if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
        return None
    call = node.exc
    if not (isinstance(call.func, ast.Name) and call.func.id == "NotImplementedError"):
        return None
    if len(call.args) != 1 or call.keywords:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith(TDD_PREFIX):
        return arg.value[len(TDD_PREFIX):]
    return None


def _body_without_docstring(fn: Func) -> list[ast.stmt]:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def stub_id(fn: Func) -> str | None:
    """The change id if `fn` is a pure stub, else None."""
    body = _body_without_docstring(fn)
    return _tdd_raise_id(body[0]) if len(body) == 1 else None


def tdd_ids_raised(fn: Func) -> set[str]:
    """Every tdd id raised anywhere in `fn` (a stub, or a malformed one with other statements)."""
    return {i for n in ast.walk(fn) if isinstance(n, ast.stmt) and (i := _tdd_raise_id(n)) is not None}


def signature(fn: Func) -> str:
    decos = "".join(f"@{ast.unparse(d)} " for d in fn.decorator_list)
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    kw = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return f"{decos}{kw} {fn.name}({ast.unparse(fn.args)}){ret}"


def parse_py(source: str, filename: str = "<src>") -> ast.Module:
    return ast.parse(source, filename=filename)


def functions(source: str) -> dict[str, Func]:
    return dict(iter_functions(parse_py(source)))


def stubs_for(source: str, cid: str) -> dict[str, str]:
    """{qualname: signature} of the stubs for change `cid` in one module."""
    return {q: signature(f) for q, f in iter_functions(parse_py(source)) if stub_id(f) == cid}


def malformed_stubs(source: str) -> list[str]:
    """Functions that raise a tdd NotImplementedError but are not pure stubs."""
    return [q for q, f in iter_functions(parse_py(source)) if tdd_ids_raised(f) and stub_id(f) is None]


def module_name(rel_file: str, pythonpath: list[str]) -> str | None:
    """Dotted module for a repo-relative .py file, via the longest matching PYTHONPATH entry."""
    path = Path(rel_file).with_suffix("")
    best: tuple[int, tuple[str, ...]] | None = None
    for entry in pythonpath:
        e = Path(entry.strip("/") or ".")
        parts = () if str(e) == "." else e.parts
        if path.parts[:len(parts)] == parts and (best is None or len(parts) > best[0]):
            best = (len(parts), path.parts[len(parts):])
    if best is None or not best[1]:
        return None
    mod = list(best[1])
    if mod[-1] == "__init__":
        mod = mod[:-1]
    return ".".join(mod) or None


# --------------------------------------------------------------------------- stub-only change
def _skeleton(tree: ast.Module, drop: set[str]) -> str:
    """Module with imports, the functions named in `drop` and empty classes removed."""
    def strip(body: list[ast.stmt], prefix: str) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and prefix + node.name in drop:
                continue
            if isinstance(node, ast.ClassDef):
                node = ast.ClassDef(**{**node.__dict__, "body": strip(node.body, f"{prefix}{node.name}.")})
            out.append(node)
        return out
    return ast.dump(ast.Module(body=strip(tree.body, ""), type_ignores=[]), include_attributes=False)


def stub_only_errors(base_src: str | None, head_src: str | None, rel: str) -> list[str]:
    """Why a change from base to head is more than adding stubs (empty when it is stub-only)."""
    if head_src is None:
        return [f"{rel}: file deleted"] if base_src is not None else []
    try:
        head = parse_py(head_src, rel)
        base = parse_py(base_src, rel) if base_src is not None else ast.Module(body=[], type_ignores=[])
    except SyntaxError as e:
        return [f"{rel}: syntax error: {e}"]
    errors: list[str] = []
    base_fns = {q: ast.dump(f, include_attributes=False) for q, f in iter_functions(base)}
    head_fns = dict(iter_functions(head))
    for q, dump in base_fns.items():
        if q not in head_fns:
            errors.append(f"{rel}: existing function {q} removed")
        elif ast.dump(head_fns[q], include_attributes=False) != dump:
            errors.append(f"{rel}: existing function {q} changed")
    added = {q for q in head_fns if q not in base_fns}
    for q in sorted(added):
        if stub_id(head_fns[q]) is None:
            errors.append(f"{rel}: added function {q} is not a stub")
        else:
            errors += _stub_header_errors(head_fns[q], q, rel)
    base_classes = _class_names(base)
    for q, cls in _iter_classes(head):
        if q not in base_classes:
            errors += _class_header_errors(cls, q, rel)
    errors += _added_import_errors(base, head, added, rel)
    if not errors:
        new_classes = _new_empty_class_names(base, head, added)
        if _skeleton(base, set()) != _skeleton(_drop_classes(head, new_classes), added):
            errors.append(f"{rel}: changes outside added stubs and imports")
    return errors


Import = ast.Import | ast.ImportFrom


def _bound_names(node: Import) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    return [a.asname or a.name for a in node.names]


def _module_names(tree: ast.Module) -> set[str]:
    """Names a module binds at top level: functions, classes, assignments and imports."""
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update(_bound_names(n))
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names.update(x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name))
    return names


def _added_import_errors(base: ast.Module, head: ast.Module, added: set[str], rel: str) -> list[str]:
    """A spec PR may add imports, but only at module level, never `*`, and never one that rebinds a
    name the module already has or the PR adds: that would change behaviour while every function
    body stays identical."""
    errors: list[str] = []
    seen = [ast.dump(n) for n in base.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    taken = _module_names(base) | {q for q in added if "." not in q}
    for n in head.body:
        if not isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        d = ast.dump(n)
        if d in seen:
            seen.remove(d)                       # an import the base already had
            continue
        for name in _bound_names(n):
            if name == "*":
                errors.append(f"{rel}: added `{ast.unparse(n)}` binds unknown names; import names explicitly")
            elif name in taken:
                errors.append(f"{rel}: added `{ast.unparse(n)}` rebinds `{name}`, which the module already defines")
    base_class_imports = {q: [ast.dump(s) for s in c.body if isinstance(s, (ast.Import, ast.ImportFrom))]
                          for q, c in _iter_classes(base)}
    for q, cls in _iter_classes(head):
        known = list(base_class_imports.get(q, []))
        for s in cls.body:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                if ast.dump(s) in known:
                    known.remove(ast.dump(s))
                else:
                    errors.append(f"{rel}: added `{ast.unparse(s)}` inside class {q}; imports go at module level")
    return errors


# A stub's header runs at import time (decorators, defaults; annotations unless postponed), and so
# do a new class's bases, keywords and decorators. Only plain names, attributes, subscripts and
# constants are allowed there, plus these decorators.
STUB_DECORATORS = {"staticmethod", "classmethod", "property", "abstractmethod", "abc.abstractmethod",
                   "overload", "typing.overload"}
_CODE_NODES = (ast.Call, ast.Lambda, ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp,
               ast.GeneratorExp, ast.Await, ast.Yield, ast.YieldFrom)


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _runs_code(nodes: list[ast.expr | None]) -> bool:
    return any(isinstance(x, _CODE_NODES) for n in nodes if n is not None for x in ast.walk(n))


def _stub_header_errors(fn: Func, q: str, rel: str) -> list[str]:
    errors = [f"{rel}: stub {q} has decorator @{ast.unparse(d)}; allowed: {', '.join(sorted(STUB_DECORATORS))}"
              for d in fn.decorator_list if _name_of(d) not in STUB_DECORATORS]
    a = fn.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)]
    header: list[ast.expr | None] = [*a.defaults, *a.kw_defaults, fn.returns, *(p.annotation for p in params)]
    if _runs_code(header):
        errors.append(f"{rel}: stub {q} runs code in its header (a call, lambda or comprehension "
                      "in a default or annotation)")
    return errors


def _class_header_errors(cls: ast.ClassDef, q: str, rel: str) -> list[str]:
    errors = [f"{rel}: new class {q} has decorator @{ast.unparse(d)}" for d in cls.decorator_list]
    if _runs_code([*cls.bases, *(k.value for k in cls.keywords)]):
        errors.append(f"{rel}: new class {q} runs code in its bases or keywords")
    return errors


def _iter_classes(tree: ast.Module) -> Iterator[tuple[str, ast.ClassDef]]:
    def walk(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, ast.ClassDef]]:
        for n in body:
            if isinstance(n, ast.ClassDef):
                yield prefix + n.name, n
                yield from walk(n.body, f"{prefix}{n.name}.")
    yield from walk(tree.body, "")


def _class_names(tree: ast.Module) -> set[str]:
    def walk(body: list[ast.stmt], prefix: str) -> Iterator[str]:
        for n in body:
            if isinstance(n, ast.ClassDef):
                yield prefix + n.name
                yield from walk(n.body, f"{prefix}{n.name}.")
    return set(walk(tree.body, ""))


def _new_empty_class_names(base: ast.Module, head: ast.Module, added: set[str]) -> set[str]:
    """New classes whose body is only added stubs, a docstring or `pass`."""
    out = set()
    base_classes = _class_names(base)

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for n in body:
            if not isinstance(n, ast.ClassDef):
                continue
            q = prefix + n.name
            if q not in base_classes and all(
                (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and f"{q}.{s.name}" in added)
                or isinstance(s, ast.Pass)
                or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                for s in n.body
            ):
                out.add(q)
            walk(n.body, q + ".")
    walk(head.body, "")
    return out


def _drop_classes(tree: ast.Module, names: set[str]) -> ast.Module:
    def strip(body: list[ast.stmt], prefix: str) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for n in body:
            if isinstance(n, ast.ClassDef):
                q = prefix + n.name
                if q in names:
                    continue
                n = ast.ClassDef(**{**n.__dict__, "body": strip(n.body, q + ".")})
            out.append(n)
        return out
    return ast.Module(body=strip(tree.body, ""), type_ignores=[])


# =========================================================================== classify
PASS, RIGHT, WRONG = "pass", "right", "wrong"
ASSERTION_TYPES = {"builtins.AssertionError", "_pytest.outcomes.Failed"}
NIE = "builtins.NotImplementedError"


def classify_exception(exc: dict[str, Any] | None, cid: str | None, *, allow_nie: bool = True) -> str:
    if not exc:
        return WRONG
    members = exc.get("members")
    if members is not None:
        return RIGHT if members and all(
            classify_exception(m, cid, allow_nie=allow_nie) == RIGHT for m in members) else WRONG
    if exc.get("assertion") or exc.get("type") in ASSERTION_TYPES:
        return RIGHT
    if allow_nie and cid and exc.get("type") == NIE and exc.get("args") == [f"tdd:{cid}"]:
        return RIGHT
    return WRONG


def classify(rec: dict[str, Any] | None, cid: str | None, *, allow_nie: bool = True) -> str:
    """Classify one test. `allow_nie=False` is the bug-fix red check: assertions only."""
    if rec is None:
        return WRONG                              # collected but never reported
    outcome = rec.get("outcome")
    if outcome == "passed":
        return PASS
    if outcome == "failed" and rec.get("when") == "call":
        return classify_exception(rec.get("exc"), cid, allow_nie=allow_nie)
    return WRONG                                  # error, skipped, xfailed, xpassed


def describe(rec: dict[str, Any] | None) -> str:
    if rec is None:
        return "not run"
    exc = rec.get("exc") or {}
    if rec.get("outcome") == "failed" and exc:
        if exc.get("members") is not None:
            return f"{exc.get('type')} with {len(exc['members'])} members"
        args = exc.get("args") or []
        return f"{exc.get('type', '?').removeprefix('builtins.')}({', '.join(map(repr, args))[:120]})"
    return f"{rec.get('outcome')} in {rec.get('when')}"
