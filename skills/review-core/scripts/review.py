"""review-core deterministic steps (stdlib only). Two subcommands:

  prep    scope -> lens selection -> gate -> blast map -> review packet(s)
          review.py prep --repo R [--target REV | A..B | --staged | --pr N] --intent "..."
                         --work W [--effort quick|standard|deep] [--no-gate]
          Writes W/{scope,lenses,gate,blast}.json and W/packet.md (deep: one
          W/packet-<lens>.md per lens). Prints a one-screen summary.

  report  merge reviewer (+ refuter) JSON into the contract report
          review.py report --work W --findings F [--findings F2 ...] [--refuter RF]
                           [--skipped "what — why" ...] [--out-dir D]
          Prints the terminal report; writes <UTC>-<sha>.json, latest.json and appends
          ledger.jsonl in D (default <repo>/.claude/review/out, self-gitignored).

Frontmatter format (lenses, profile.md): `key: <JSON value>` lines between `---` fences;
an indented line continues the previous key; non-JSON values are kept as strings.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
GLOBAL_LENS_DIR = SKILL_DIR / "lenses"
RUBRIC_PATH = SKILL_DIR / "rubric.md"
PACK_REL = Path(".claude") / "review"
PROOF_BUDGET = {"quick": 0, "standard": 3, "deep": 8}

SEV = ["P0", "P1", "P2", "P3"]
EVIDENCE = ["PROVEN", "TRACED", "INFERRED"]
BLAST = ["LOCAL", "MODULE", "CROSS", "CONTRACT"]
MERGE_LINES = 5                                         # merge window for different-id findings
REQUIRED = ["id", "severity", "evidence", "blast", "lens", "file", "line", "summary", "failure", "fix", "proof"]
P3_CAP = 5


# =========================================================================== helpers
def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body). No frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}, text
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    raw: dict[str, str] = {}
    key = None
    for line in parts[0][3:].splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if m and not line[0].isspace():
            key = m.group(1)
            raw[key] = m.group(2)
        elif key is not None:
            raw[key] += " " + line.strip()
    meta: dict[str, Any] = {}
    for k, v in raw.items():
        try:
            meta[k] = json.loads(v.strip())
        except json.JSONDecodeError:
            meta[k] = v.strip().strip("'\"")
    return meta, body.lstrip("\n")


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    return split_frontmatter(path.read_text(encoding="utf-8"))


def expand_braces(pattern: str) -> list[str]:
    """`a/{x,y}.py` -> [`a/x.py`, `a/y.py`] (repeated groups supported)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[: m.start()], pattern[m.end():]
    return [p for alt in m.group(1).split(",") for p in expand_braces(head + alt + tail)]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    i, rx = 0, ""
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            rx, i = rx + ".*", i + 2
        else:
            c = pattern[i]
            rx += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
            i += 1
    return re.compile(rx + r"\Z")


def glob_match(path: str, patterns: list[str] | str) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    path = path.replace("\\", "/")
    return any(_glob_to_regex(p).match(path) for pat in patterns for p in expand_braces(pat))


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(obj: Any, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ensure_ignored(out_dir: Path) -> None:
    """Make the report dir ignore itself, so it never shows up in `git status`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    gi = out_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("# review-core reports (local only)\n*\n", encoding="utf-8")


def pack_dir(repo: Path) -> Path | None:
    d = repo / PACK_REL
    return d if (d / "profile.md").exists() else None


def load_profile(repo: Path) -> tuple[dict[str, Any], str]:
    d = pack_dir(repo)
    return read_frontmatter(d / "profile.md") if d else ({}, "")


def load_suppressed_ids(repo: Path) -> list[str]:
    return list(load_suppressions(repo))


def load_suppressions(repo: Path) -> dict[str, str]:
    """id -> 'reason — note — date' from `- <id> — <reason> — <note> — <date>` lines."""
    path = repo / PACK_REL / "suppressions.md"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^-\s+(\S+)\s+[—-]\s*(.*)$", line)
        if m and ":" in m.group(1):
            out[m.group(1)] = m.group(2).strip()
    return out


# =========================================================================== scope
def default_branch(repo: Path) -> str:
    ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).strip()
    if ref:
        return ref.rsplit("/", 1)[-1]
    for name in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", name, check=False).strip():
            return name
    return "HEAD"


def new_path(numstat_path: str) -> str:
    """Rename notation -> new path: `a/{x.py => y.py}` -> `a/y.py`, `x.py => y.py` -> `y.py`."""
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", numstat_path)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    return numstat_path.split(" => ")[-1]


QUOTE_OFF = ("-c", "core.quotePath=false")             # non-ASCII names stay readable
DIFF_U0 = ("--src-prefix=a/", "--dst-prefix=b/", "--no-ext-diff", "-U0")   # pin prefixes against diff.noprefix
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def git_path(raw: str) -> str:
    """Undo git's C-style quoting (`"dir/a\\"b.py"`, octal UTF-8 bytes) of a path in diff output."""
    if not (len(raw) >= 2 and raw[0] == raw[-1] == '"'):
        return raw
    s, out, i = raw[1:-1], bytearray(), 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            if re.fullmatch(r"[0-7]{3}", s[i + 1:i + 4]):
                out.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
            out.append(_C_ESCAPES.get(s[i + 1], ord(s[i + 1])))
            i += 2
            continue
        out += s[i].encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def diff_new_path(header: str) -> str | None:
    """`+++ b/path` (git adds a trailing tab when the name has a space) -> path; /dev/null -> None."""
    p = git_path(header[4:].rstrip("\t"))
    return None if p == "/dev/null" else p.removeprefix("b/")


def _numstat(repo: Path, diff_args: list[str]) -> list[dict[str, Any]]:
    files = []
    for line in git(repo, *QUOTE_OFF, *diff_args, "--numstat").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, path = parts
            files.append({"path": new_path(git_path(path)), "added": int(add) if add.isdigit() else 0,
                          "removed": int(rem) if rem.isdigit() else 0})
    return files


# =========================================================================== PR / MR targets
PR_URL_PATTERNS = (
    ("github", re.compile(r"^https://(?P<host>[^/\s]+)/(?P<project>[^/\s]+/[^/\s]+?)(?:\.git)?/pull/(?P<n>\d+)/?$")),
    ("gitlab", re.compile(r"^https://(?P<host>[^/\s]+)/(?P<project>[^\s]+?)/-/merge_requests/(?P<n>\d+)/?$")),
)
PR_REFSPECS = {"github": "pull/{n}/head", "gitlab": "merge-requests/{n}/head"}


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def parse_pr(spec: str) -> tuple[str | None, str | None, str | None, int]:
    """`spec` is a PR/MR URL or a bare number -> (kind, host, project, number). Strict: anything
    else raises, so nothing user-supplied ever reaches git except a validated number."""
    if spec.isdigit():
        return None, None, None, int(spec)
    for kind, rx in PR_URL_PATTERNS:
        if m := rx.match(spec.strip()):
            return kind, m["host"].lower(), m["project"].lower(), int(m["n"])
    raise SystemExit(f"--pr: not a PR/MR URL or number: {spec!r} "
                     "(expected https://<host>/<owner>/<repo>/pull/N or https://<host>/<group>/<repo>/-/merge_requests/N)")


def origin_project(repo: Path) -> tuple[str | None, str | None]:
    """(host, project) of the origin remote for https and ssh forms; (None, None) for local paths."""
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    m = (re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url)
         or re.match(r"^(?:ssh://)?[^@]+@([^:/]+)[:/](.+?)(?:\.git)?/?$", url))
    return (m.group(1).lower(), m.group(2).lower()) if m else (None, None)


def fetch_pr(repo: Path, spec: str, base_branch: str | None) -> dict[str, Any]:
    """Fetch a PR/MR head and its base from origin (objects only; no branch or checkout changes)."""
    kind, host, project, n = parse_pr(spec)
    o_host, o_project = origin_project(repo)
    if host and o_host and (host, project) != (o_host, o_project):
        raise SystemExit(f"--pr: {spec} is {host}/{project}, but this repo's origin is {o_host}/{o_project}")
    kinds = [kind] if kind else ["gitlab", "github"] if o_host and "gitlab" in o_host else ["github", "gitlab"]
    head = None
    for k in kinds:
        if _git_ok(repo, "fetch", "--no-tags", "--quiet", "origin", PR_REFSPECS[k].format(n=n)):
            head, kind = git(repo, "rev-parse", "FETCH_HEAD").strip(), k
            break
    if head is None:
        raise SystemExit(f"--pr: could not fetch PR/MR {n} from origin (tried {', '.join(kinds)} refs)")
    base_name = base_branch or default_branch(repo)
    git(repo, "fetch", "--no-tags", "--quiet", "origin", base_name)
    base_sha = git(repo, "rev-parse", "FETCH_HEAD").strip()
    merge_base = git(repo, "merge-base", base_sha, head).strip()
    merged_note = None
    if merge_base == head:                               # already merged: diff against the pre-merge base
        for line in git(repo, "log", "--merges", "--ancestry-path", "--reverse", "--format=%H %P",
                        f"{head}..{base_sha}").splitlines():
            commit, *parents = line.split()
            if len(parents) >= 2 and head in parents[1:]:
                merge_base = git(repo, "merge-base", parents[0], head).strip()
                merged_note = f"already merged in {commit[:7]}; reviewed against its pre-merge base {parents[0][:7]}"
                break
    return {"kind": kind, "number": n, "spec": spec, "head": head, "base_branch": base_name,
            "base_sha": base_sha, "merge_base": merge_base, "merged": merged_note}


def make_pr_worktree(repo: Path, pr: dict[str, Any], work: Path) -> tuple[Path, list[str]]:
    """Detached temp worktree at the PR head (git metadata only; the user's checkout is untouched).
    The review pack always comes from the user's checkout: the PR's own .claude/review/ is the
    author's (suppressions, gate commands, lens text), so it is removed, never trusted. snapshot_links
    are linked."""
    import shutil
    wt = work / f"pr-{pr['number']}-worktree"
    if wt.exists():
        remove_pr_worktree(repo, wt, [])
    git(repo, "-c", "core.longpaths=true", "worktree", "add", "--detach", "--quiet", str(wt), pr["head"])
    if (wt / PACK_REL).exists():
        shutil.rmtree(wt / PACK_REL)
    if (repo / PACK_REL).is_dir():
        shutil.copytree(repo / PACK_REL, wt / PACK_REL, ignore=shutil.ignore_patterns("out"))
    meta, _ = load_profile(repo)
    links = []
    for rel in meta.get("snapshot_links", []):
        src, link = repo / rel, wt / rel
        if src.exists() and not link.exists():
            if sys.platform == "win32":
                import _winapi
                _winapi.CreateJunction(str(src), str(link))
            else:
                os.symlink(src, link, target_is_directory=True)
            links.append(str(link))
    return wt, links


def remove_pr_worktree(repo: Path, wt: Path, links: list[str]) -> None:
    for link in links:                                   # unlink first: never recurse into linked data
        if Path(link).exists():
            os.rmdir(link) if sys.platform == "win32" else os.unlink(link)
    git(repo, "worktree", "remove", "--force", str(wt), check=False)
    git(repo, "worktree", "prune", check=False)


def resolve_scope(repo: Path, target: str | None, staged: bool) -> dict[str, Any]:
    """No target: feature branch -> merge-base(default, HEAD) vs working tree; default branch
    -> working tree vs HEAD if dirty, else HEAD. Untracked files count in working-tree scopes."""
    head = git(repo, "rev-parse", "HEAD").strip()
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    base = default_branch(repo)
    working_tree = False
    if staged:
        desc, diff_args = "staged changes", ["diff", "--cached"]
    elif target and ".." in target:
        a, b = target.split("..", 1)
        desc, diff_args = target, ["diff", a, b or "HEAD"]
    elif target:
        head = git(repo, "rev-parse", target).strip()
        desc, diff_args = target, ["diff", f"{head}^", head]
    else:
        dirty = bool(git(repo, "status", "--porcelain").strip())
        if branch not in (base, "HEAD") and base != "HEAD":
            mb = git(repo, "merge-base", base, "HEAD").strip()
            desc, diff_args, working_tree = f"{branch} vs {base} (+uncommitted)", ["diff", mb], True
        elif dirty:
            desc, diff_args, working_tree = "uncommitted changes", ["diff", "HEAD"], True
        else:
            desc, diff_args = "HEAD (clean default branch)", ["diff", "HEAD^", "HEAD"]
    files = _numstat(repo, diff_args)
    if working_tree:
        for path in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
            if path and not any(f["path"] == path for f in files) and (repo / path).is_file():
                try:                                    # nested repos are listed as `sub/` directories
                    n = len((repo / path).read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    continue
                files.append({"path": path, "added": n, "removed": 0, "untracked": True})
    return {"repo": str(repo), "target": desc, "head_sha": head, "base": base, "diff_args": diff_args,
            "diff_cmd": f"git -C {repo} {' '.join(diff_args)}", "files": files, "working_tree": working_tree,
            "branch": branch if working_tree and branch != "HEAD" else None,   # rounds chain by this
            "changed_lines": sum(f["added"] + f["removed"] for f in files)}


# =========================================================================== lenses
def available_lenses(repo: Path) -> dict[str, dict[str, Any]]:
    """Global lenses, then pack lenses (a same-named pack lens replaces the global one)."""
    lenses: dict[str, dict[str, Any]] = {}
    for source, d in (("global", GLOBAL_LENS_DIR), ("pack", repo / PACK_REL / "lenses")):
        for path in sorted(d.glob("*.md")) if d.is_dir() else []:
            meta, _ = read_frontmatter(path)
            name = meta.get("name", path.stem)
            lenses[name] = {"name": name, "path": str(path), "source": source,
                            "applies_to": meta.get("applies_to", ["**"])}
    return lenses


def select_lenses(scope: dict[str, Any]) -> list[dict[str, Any]]:
    paths = [f["path"] for f in scope["files"]]
    chosen = []
    for lens in available_lenses(Path(scope["repo"])).values():
        hits = [p for p in paths if glob_match(p, lens["applies_to"])]
        if hits:
            chosen.append({**lens, "matched": hits})
    return chosen


# =========================================================================== gate
def scoped_tests(paths: list[str], tests_map: dict[str, list[str]]) -> list[str]:
    dirs: list[str] = []
    for pattern, targets in tests_map.items():
        if any(glob_match(p, pattern) for p in paths):
            dirs += [t for t in targets if t not in dirs]
    return dirs


def select_tests(scope: dict[str, Any], graph: Any | None, tests_map: dict[str, list[str]]) -> tuple[list[str], str]:
    """Test files that import the changed modules (via the code graph), plus changed test files;
    falls back to the profile's `tests_map` dirs when the graph finds none."""
    paths = [f["path"] for f in scope["files"]]
    if graph is not None:
        import codemap
        changed = {m for p in paths if p.endswith(".py") and (m := graph.module_of(p))}
        found = set(codemap.importing_tests(graph, changed))
        found |= {p for p in paths if codemap.kind_of(p) == "test" and p.rsplit("/", 1)[-1].startswith("test_")
                  and (Path(scope["repo"]) / p).exists()}
        if found:
            return sorted(found), "tests importing the changed modules"
    return scoped_tests(paths, tests_map), "profile tests_map (no importing tests found)"


SHELL_SAFE_PATH_RX = re.compile(r"^[\w./\\-]+$")


def shell_paths(paths: list[str], dropped: list[str]) -> str:
    """Paths joined for a shell command template. The gate runs through the shell (cmd.exe or sh,
    whose quoting differs), so a name with anything beyond word chars . / \\ - is left out and
    recorded in `dropped` instead of being quoted."""
    dropped += [p for p in paths if not SHELL_SAFE_PATH_RX.match(p)]
    return " ".join(p for p in paths if SHELL_SAFE_PATH_RX.match(p))


def run_gate(scope: dict[str, Any], timeout: int = 300, graph: Any | None = None) -> dict[str, Any]:
    """Profile `gate` commands with {python} {changed_py} {tests} {repo}; empty placeholder -> skipped.
    Non-zero exits whose output matches a profile `gate_env_failures` regex are labelled
    environment gaps rather than left unattributed."""
    repo = Path(scope["repo"])
    meta, _ = load_profile(repo)
    if not meta.get("gate"):
        return {"results": [], "note": "no pack gate configured"}
    paths = [f["path"] for f in scope["files"]]
    tests, tests_how = select_tests(scope, graph, meta.get("tests_map", {}))
    dropped: list[str] = []
    subs = {"python": meta.get("python", "python"), "repo": str(repo),
            "changed_py": shell_paths([p for p in paths if p.endswith(".py") and (repo / p).exists()], dropped),
            "tests": shell_paths(tests, dropped)}
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           **{k: str(v).format(**subs) for k, v in meta.get("env", {}).items()}}
    env_rx = [re.compile(p) for p in meta.get("gate_env_failures", [])]
    results: list[dict[str, Any]] = []
    for template in meta["gate"]:
        needs = [k for k in ("changed_py", "tests") if "{" + k + "}" in template]
        if any(not subs[k] for k in needs):
            results.append({"cmd": template, "exit": None, "skipped": f"no {needs[0]} for this scope"})
            continue
        cmd = template.format(**subs)
        code, output = _run_with_timeout(cmd, repo, env, timeout)
        if code is None:
            results.append({"cmd": cmd, "exit": None, "skipped": f"timeout after {timeout}s (process tree killed)"})
            continue
        res: dict[str, Any] = {"cmd": cmd, "exit": code, "tail": output.strip().splitlines()[-8:]}
        if code and (hit := next((rx.pattern for rx in env_rx if rx.search(output)), None)):
            res["environment"] = f"output matches env-failure pattern {hit!r}"
        results.append(res)
    out: dict[str, Any] = {"results": results, "tests_selected_by": tests_how}
    if dropped:
        out["unsafe_paths_skipped"] = sorted(set(dropped))
    return out


def _run_with_timeout(cmd: str, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, str]:
    """Shell command with stdin closed and its own process group, so a hung grandchild (conda ->
    python -> pytest) can't block on inherited stdin and a timeout kills the whole tree."""
    windows = os.name == "nt"
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
                            start_new_session=not windows)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, 9)
        proc.communicate()
        return None, ""


# =========================================================================== blast map
DEF_RX = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


NEW_RANGE_RX = re.compile(r"^@@ [^@]*\+(\d+)(?:,(\d+))? @@")


def changed_symbols(repo: Path, scope: dict[str, Any], graph: Any | None = None) -> dict[str, set[str]]:
    """Qualified names of the defs enclosing every changed line (via the code graph), plus
    def/class names on added lines; untracked files contribute all their defs."""
    out: dict[str, set[str]] = defaultdict(set)
    if scope.get("diff_args"):
        import codemap
        current = None
        for line in git(repo, *QUOTE_OFF, *scope["diff_args"], *DIFF_U0).splitlines():
            if line.startswith("+++ "):
                current = diff_new_path(line)
            elif current and (m := NEW_RANGE_RX.match(line)):
                start, count = int(m.group(1)), int(m.group(2) or 1)
                for ln in range(start, start + max(count, 1)):
                    if graph is not None and (enc := codemap.enclosing(graph, current, ln)):
                        out[current].add(enc.qualname)
            elif current and line.startswith("+") and (d := DEF_RX.match(line[1:])):
                out[current].add(d.group(1))
    for f in scope["files"]:
        if f.get("untracked") and (repo / f["path"]).exists():
            for line in (repo / f["path"]).read_text(encoding="utf-8", errors="replace").splitlines():
                if d := DEF_RX.match(line):
                    out[f["path"]].add(d.group(1))
    for path, names in out.items():                     # drop a bare name when its qualname is present
        out[path] = {n for n in names if not any(q != n and q.endswith("." + n) for q in names)}
    return out


def _subsystem(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


BLAST_RANK = ["LOCAL", "MODULE", "CROSS", "CONTRACT"]


def blast_map(scope: dict[str, Any], graph: Any | None = None) -> dict[str, Any]:
    """Transitive caller tree per changed symbol (depth 3, via the code graph) with a computed
    reach level and its justifying path; per changed file: direct importers and, when the pack
    has a built code map, the file's TRUNK/BRANCH/LEAF tier and reason."""
    import codemap
    repo = Path(scope["repo"])
    if graph is None:
        graph = codemap.build_graph(repo, [f["path"] for f in scope["files"] if f.get("untracked")])
    meta, _ = load_profile(repo)
    contract = meta.get("contract_surfaces", [])
    paths = [f["path"] for f in scope["files"]]
    entries = []
    for path, names in sorted(changed_symbols(repo, scope, graph).items()):
        if codemap.kind_of(path) == "test":
            continue                                     # tests are counted as callers, not blasted
        for name in sorted(names):
            tree = codemap.callers(graph, path, name, contract)
            entries.append({"file": path, "symbol": name, "private": name.rsplit(".", 1)[-1].startswith("_"),
                            "usages": tree["nodes"], **tree})
    modules = []
    for path in paths:
        mod = graph.module_of(path) if path.endswith(".py") else None
        if mod is None:
            continue
        importers = graph.dependents(mod)
        prod = sorted(graph.paths[m] for m in importers if codemap.kind_of(graph.paths[m]) != "test")
        modules.append({"file": path, "module": mod, "importer_files": len(prod),
                        "test_importers": len(importers) - len(prod),
                        "by_subsystem": dict(Counter(_subsystem(p) for p in prod)), "sample": prod[:15]})
    cmap = codemap.load_map(repo)
    tiers = {}
    for path in paths:
        key = codemap.module_name(path) if path.endswith(".py") else path
        entry = (cmap or {}).get("modules", {}).get(key)
        m = (entry or {}).get("metrics", {})
        tiers[path] = ({"tier": entry["tier"], "reason": entry["reason"],
                        "tests": m.get("test_coverage"), "tests_reach": m.get("tests_reach")} if entry
                       else {"tier": "UNMAPPED", "reason": ""})
    status = ({"present": False, "stale": None, "note": "no codemap.json — run `codemap.py build`"} if cmap is None
              else {"present": True, "built_at": cmap["built_at"], "declared": cmap.get("declared", False),
                    "stale": cmap["edge_hash"] != graph.edge_hash(tracked_only=True)})   # map is built from tracked files
    if status.get("stale"):
        status["note"] = "import graph changed since the map was built — run `codemap.py build`"
    return {"symbols": entries, "modules": modules, "tiers": tiers, "codemap": status,
            "reach": max((e["reach"] for e in entries), key=BLAST_RANK.index, default="LOCAL"),
            "contract_surfaces_touched": [p for p in paths if glob_match(p, contract)],
            "hotspots_touched": [p for p in paths if glob_match(p, meta.get("hotspots", []))]}


# =========================================================================== packet
def _diff_text(scope: dict[str, Any], max_lines: int) -> str:
    repo = Path(scope["repo"])
    if not scope.get("diff_args"):
        return f"(run `{scope['diff_cmd']}` — not embedded for this scope)"
    lines = git(repo, *scope["diff_args"]).splitlines()
    for f in scope["files"]:
        if f.get("untracked"):
            body = (repo / f["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
            lines += [f"+++ new untracked file: {f['path']}", *("+" + b for b in body)]
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"... [{len(lines) - max_lines} more diff lines truncated — run the diff command]"]
    return "\n".join(lines)


def build_packet(scope: dict[str, Any], lenses: list[dict[str, Any]], blast: dict[str, Any], intent: str,
                 gate: dict[str, Any] | None, proof_budget: int, max_diff_lines: int = 1500,
                 focus: str | None = None, diff_tests: dict[str, Any] | None = None) -> str:
    repo = Path(scope["repo"])
    meta, profile_body = load_profile(repo)
    files = "\n".join(f"- `{f['path']}` (+{f['added']}/-{f['removed']}{', untracked' if f.get('untracked') else ''})"
                      for f in scope["files"])
    out = ["# Review packet", f"**Intent:** {intent}",
           f"**Target:** {scope['target']} · **Head:** `{scope['head_sha']}` · **Diff:** `{scope['diff_cmd']}` · "
           f"**Proof budget:** {proof_budget} commands", f"**Python:** `{meta.get('python', 'python')}`",
           "## Changed files", files or "(none)"]
    if focus:
        out = [out[0], "## Consumer focus (your assignment)", focus, *out[1:]]
    tiers = blast.get("tiers", {})
    if tiers:
        out += ["## Where the change lands (code map tiers)"] + [
            f"- `{p}` — **{t['tier']}** {t['reason']}"
            + (f" · tests: {t['tests']} ({t['tests_reach']} test files reach it)" if t.get("tests") else "")
            for p, t in tiers.items()]
        if blast["codemap"].get("note"):
            out.append(f"_{blast['codemap']['note']}_")
    slim = {k: v for k, v in blast.items() if k not in ("tiers", "codemap")}
    slim["symbols"] = [{k: v for k, v in s.items() if k != "nodes_detail"} for s in blast["symbols"]]
    out += [f"## Blast map (measured reach: **{blast.get('reach', 'LOCAL')}**)", "```json",
            json.dumps(slim, indent=1), "```"]
    if diff_tests:
        out += ["## Differential tests (base vs change, already run)", _diff_tests_line(diff_tests) or "",
                *(f"- `{x['test']}`: {x['base']} → {x['change']}" for x in diff_tests.get("changed", []))]
    if gate and gate.get("results"):
        out += [f"## Gate already run (don't re-run; tests: {gate.get('tests_selected_by', '?')})"] + [
            f"- `{r['cmd']}` → " + (f"exit {r['exit']}" + (f" ({r['environment']})" if r.get("environment") else "")
                                    if r.get("exit") is not None else r.get("skipped", "?"))
            for r in gate["results"]]
    out += ["## Rubric", RUBRIC_PATH.read_text(encoding="utf-8").split("\n", 1)[1].strip(), "## Lenses in scope"]
    for lens in lenses:
        out += [f"### Lens `{lens['name']}` ({lens['source']})", read_frontmatter(Path(lens["path"]))[1].strip()]
    if profile_body:
        out += ["## Project profile", profile_body.strip()]
    ids = load_suppressed_ids(repo)
    out += ["## Suppressed ids (don't re-raise)", "\n".join(f"- `{i}`" for i in ids) or "(none)",
            "## Diff", "```diff", _diff_text(scope, max_diff_lines), "```"]
    return "\n\n".join(out) + "\n"


# =========================================================================== deep
QUALIFYING_REACH = ("CROSS", "CONTRACT")
DIFF_TEST_DEFAULT = "{python} -m pytest -q -p no:cacheprovider --junitxml={junit} {tests}"


def deep_qualification(blast: dict[str, Any]) -> tuple[bool, str]:
    """Deep is worth it only for TRUNK files, CROSS/CONTRACT reach or contract surfaces."""
    tiers = blast.get("tiers", {})
    trunk = [p for p, t in tiers.items() if t["tier"] == "TRUNK"]
    reasons = []
    if trunk:
        reasons.append(f"TRUNK touched ({', '.join(trunk[:3])})")
    if blast.get("reach") in QUALIFYING_REACH:
        reasons.append(f"reach {blast['reach']}")
    if blast.get("contract_surfaces_touched"):
        reasons.append("contract surface touched")
    if reasons:
        return True, "; ".join(reasons)
    seen = "/".join(sorted({t["tier"] for t in tiers.values()})) or "no tiers"
    note = "" if blast.get("codemap", {}).get("present") else " (no code map: tier unavailable)"
    return False, f"not qualified: {seen} only, reach {blast.get('reach', 'LOCAL')}{note}"


def select_impact_targets(blast: dict[str, Any], repo: Path, k: int = 3) -> list[dict[str, Any]]:
    """Downstream consumer groups (by subsystem) from the caller tree, outside the changed
    files' own directories, ranked: exposure root/contract +3, TRUNK file +2, node count light."""
    import codemap
    cmap = codemap.load_map(repo) or {"modules": {}}

    def tier(path: str) -> str:
        return cmap["modules"].get(codemap.module_name(path), {}).get("tier", "")

    origin_dirs = {s["file"].rsplit("/", 1)[0] for s in blast["symbols"]}
    groups: dict[str, dict[str, Any]] = {}
    for s in blast["symbols"]:
        for n in s.get("nodes_detail", []):
            if n["file"].rsplit("/", 1)[0] in origin_dirs:
                continue
            g = groups.setdefault(n["subsystem"], {"subsystem": n["subsystem"], "files": set(), "paths": [],
                                                   "nodes": 0, "root": False, "trunk": False})
            g["nodes"] += 1
            g["files"].add(n["file"])
            g["root"] |= bool(n["is_root"] or n["contract"])
            g["trunk"] |= tier(n["file"]) == "TRUNK"
            if len(g["paths"]) < 4 and n["path"] not in g["paths"]:
                g["paths"].append(n["path"])
    for g in groups.values():
        g["score"] = 3 * g["root"] + 2 * g["trunk"] + min(g["nodes"], 10) / 10
        g["files"] = sorted(g["files"])[:8]
    return sorted(groups.values(), key=lambda g: -g["score"])[:k]


def _impact_focus(target: dict[str, Any]) -> str:
    return "\n".join([
        f"**Subsystem:** `{target['subsystem']}`"
        f"{' · reaches an exposure root/contract' if target['root'] else ''}{' · TRUNK' if target['trunk'] else ''}",
        "**Call paths from the change into this consumer:**", *(f"- `{p}`" for p in target["paths"]),
        "**Consumer files to read in full:**", *(f"- `{f}`" for f in target["files"]),
        "**Question:** does this change violate any assumption this consumer makes — units, sign, invariants "
        "(e.g. ES ≥ VaR), None/empty handling, ordering, persisted shape? Report only consumer-side breakage "
        "the diff alone would not show; the breadth reviewer covers the diff itself."])


def base_ref(scope: dict[str, Any]) -> tuple[str | None, str | None]:
    """(base ref, change ref) for the differential run; change ref None = the working tree."""
    da = scope.get("diff_args") or []
    if not da:
        return None, None
    if da == ["diff", "--cached"]:
        return "HEAD", None
    if scope.get("working_tree"):
        return da[1], None
    return da[1], (da[2] if len(da) > 2 else "HEAD")


def snapshot(repo: Path, ref: str, dest: Path, links: list[str]) -> list[Path]:
    """`git archive <ref>` extracted into `dest` (no git state change) + links to gitignored dirs."""
    import io
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
        tar.extractall(dest, filter="data")
    made = []
    for rel in links:
        src, link = repo / rel, dest / rel
        if not src.exists() or link.exists():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(src), str(link))
        else:
            os.symlink(src, link, target_is_directory=True)
        made.append(link)
    return made


def remove_snapshot(dest: Path, links: list[Path]) -> None:
    """Unlink the links FIRST so removing the snapshot can never touch the linked real dirs."""
    import shutil
    for link in links:
        if sys.platform == "win32":
            os.rmdir(link)                               # removes the junction, not its target
        else:
            os.unlink(link)
    shutil.rmtree(dest, ignore_errors=True)


def _junit_outcomes(path: Path) -> dict[str, str]:
    import xml.etree.ElementTree as ET
    if not path.exists():
        return {}
    out = {}
    for tc in ET.parse(path).iter("testcase"):
        kids = {c.tag for c in tc}
        out[f"{tc.get('classname', '')}::{tc.get('name', '')}"] = (
            "failed" if "failure" in kids else "error" if "error" in kids else "skipped" if "skipped" in kids
            else "passed")
    return out


def run_diff_tests(scope: dict[str, Any], graph: Any, work: Path, timeout: int = 300) -> dict[str, Any]:
    """Run the import-selected tests on the base and on the change; compare per-test outcomes."""
    repo = Path(scope["repo"])
    meta, _ = load_profile(repo)
    tests, how = select_tests(scope, graph, meta.get("tests_map", {}))
    base, change = base_ref(scope)
    if not tests or base is None:
        return {"verdict": "inconclusive", "why": "no tests selected" if not tests else "no base ref for scope"}
    env_rx = [re.compile(p) for p in meta.get("gate_env_failures", [])]
    template = meta.get("diff_test", DIFF_TEST_DEFAULT)
    runs: dict[str, dict[str, Any]] = {}
    for side, ref in (("base", base), ("change", change)):
        root, links = repo, []
        if ref is not None:
            root = work / f"snapshot-{side}"
            links = snapshot(repo, ref, root, meta.get("snapshot_links", []))
        try:
            junit = work / f"junit-{side}.xml"
            junit.unlink(missing_ok=True)               # a run that dies early must not read the last run's XML
            present = [t for t in tests if (root / t).exists()]
            subs = {"python": meta.get("python", "python"), "repo": str(root), "junit": str(junit),
                    "tests": shell_paths(present, [])}
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           **{k: str(v).format(**subs) for k, v in meta.get("env", {}).items()}}
            code, output = (_run_with_timeout(template.format(**subs), root, env, timeout) if present
                            else (None, "no selected test file exists on this side"))
            runs[side] = {"ref": ref or "working tree", "exit": code, "outcomes": _junit_outcomes(junit),
                          "environment": next((rx.pattern for rx in env_rx if rx.search(output)), None),
                          "tail": output.strip().splitlines()[-4:]}
        finally:
            if ref is not None:
                remove_snapshot(root, links)
    b, c = runs["base"]["outcomes"], runs["change"]["outcomes"]
    changed = [{"test": t, "base": b.get(t, "absent"), "change": c.get(t, "absent")}
               for t in sorted(set(b) | set(c)) if b.get(t) != c.get(t)]
    errored = all(v == "error" for v in [*b.values(), *c.values()]) if (b or c) else True
    if changed:
        verdict = "behaviour change observed"
    elif errored and (runs["base"]["environment"] or runs["change"]["environment"] or not (b or c)):
        verdict = "inconclusive"
    else:
        verdict = "no test distinguishes the change"
    same = [t for t in set(b) & set(c) if b[t] == c[t]]
    stable = {"passed_both": sum(b[t] == "passed" for t in same),
              "broken_both": sum(b[t] in ("failed", "error") for t in same)}
    return {"verdict": verdict, "tests": tests, "tests_selected_by": how, "base_ref": base, "stable": stable,
            "counts": {side: dict(Counter(r["outcomes"].values())) for side, r in runs.items()},
            "changed": changed[:25], "changed_count": len(changed),
            "runs": {side: {k: v for k, v in r.items() if k != "outcomes"} for side, r in runs.items()}}


def _diff_tests_line(d: dict[str, Any] | None) -> str | None:
    if not d:
        return None
    if d["verdict"] == "inconclusive" and "counts" not in d:
        return f"Differential tests: inconclusive ({d.get('why')})"
    total = sum(d["counts"].get("change", {}).values())
    detail = ", ".join(f"{x['test'].rsplit('::', 1)[-1]}: {x['base']}→{x['change']}" for x in d["changed"][:3])
    env = [s for s, r in d["runs"].items() if r.get("environment")]
    st = d.get("stable", {})
    scope_note = ""
    if d["verdict"] == "no test distinguishes the change" and st.get("broken_both"):
        scope_note = (f" (among the {st.get('passed_both', 0)} passing on both sides; "
                      f"{st['broken_both']} fail/error on both and compare nothing)")
    return (f"Differential tests (base `{d['base_ref'][:12]}`): {len(d['tests'])} files, {total} tests — "
            f"{d['verdict']}" + scope_note
            + (f" ({d['changed_count']} changed: {detail})" if d["changed"] else "")
            + (f" · environment failures on {', '.join(env)}" if env else ""))


PR_CODE_NOTE = "{} skipped: PR code is not executed without --run-pr-code"


def cmd_prep(a: argparse.Namespace) -> None:
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    origin = Path(a.repo).resolve()
    if a.pr:
        pr = fetch_pr(origin, a.pr, a.base)
        wt, links = make_pr_worktree(origin, pr, work)
        scope = resolve_scope(wt, f"{pr['merge_base']}..{pr['head']}", False)
        label = "MR" if pr["kind"] == "gitlab" else "PR"
        scope.update(target=f"{label} {pr['number']} ({pr['base_branch']}..{pr['head'][:7]})", head_sha=pr["head"],
                     pr=pr, temp_worktree=str(wt), links=links, origin_repo=str(origin),
                     commits=git(origin, "log", "--format=%s", f"{pr['merge_base']}..{pr['head']}").splitlines()[:20])
    else:
        scope = resolve_scope(origin, a.target, a.staged)
    scope["intent"], scope["effort"] = a.intent, a.effort
    scope["line_map"] = {p: sorted(v) for p, v in _changed_lines(scope).items()}
    lenses = select_lenses(scope)
    import codemap
    graph = codemap.build_graph(Path(scope["repo"]), [f["path"] for f in scope["files"] if f.get("untracked")])
    untrusted = bool(a.pr) and not a.run_pr_code       # a PR checkout is someone else's code: never run it by default
    gate: dict[str, Any] = ({"results": [], "note": "gate skipped (--no-gate)"} if a.no_gate
                            else {"results": [], "note": PR_CODE_NOTE.format("gate")} if untrusted
                            else run_gate(scope, graph=graph))
    blast = blast_map(scope, graph)
    effort = a.effort
    if a.effort == "deep":
        qualifies, why = deep_qualification(blast)
        if a.force_deep:
            scope["deep"] = f"forced ({why})"
        elif qualifies:
            scope["deep"] = f"qualified: {why}"
        else:
            effort = "standard"
            scope["deep"] = f"deep requested → standard ({why})"
    scope["effort"] = effort
    scope["prepared_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    targets: list[dict[str, Any]] = []
    diff: dict[str, Any] | None = None
    if effort == "deep":
        targets = select_impact_targets(blast, Path(scope["repo"]))
        diff = ({"verdict": "inconclusive", "why": PR_CODE_NOTE.format("differential tests")} if untrusted
                else run_diff_tests(scope, graph, work))
    for name, obj in (("scope", scope), ("lenses", lenses), ("gate", gate), ("blast", blast),
                      ("impact", targets), ("diff_tests", diff)):
        dump_json(obj, work / f"{name}.json")
    budget = PROOF_BUDGET[effort]
    packets = {"packet.md": build_packet(scope, lenses, blast, a.intent, gate, budget, diff_tests=diff)}
    for i, t in enumerate(targets, 1):
        packets[f"packet-impact-{i}.md"] = build_packet(scope, lenses, blast, a.intent, gate, budget,
                                                        focus=_impact_focus(t), diff_tests=diff)
    for fname, text in packets.items():
        (work / fname).write_text(text, encoding="utf-8")
    print(json.dumps({
        "target": scope["target"], "head_sha": scope["head_sha"], "files": len(scope["files"]),
        "effort": effort, "deep": scope.get("deep"),
        **({"review_repo": scope["repo"], "commits": scope["commits"],
            "note_pr": "agents read the PR code in review_repo (a temp worktree); `report` removes it",
            **({"merged": scope["pr"]["merged"]} if scope["pr"].get("merged") else {})}
           if scope.get("temp_worktree") else {}),
        **({"note": "empty scope: the target changes no files (e.g. a no-op merge) — nothing to review"}
           if not scope["files"] else {}),
        "impact_targets": [{"subsystem": t["subsystem"], "score": t["score"]} for t in targets],
        "diff_tests": _diff_tests_line(diff),
        "changed_lines": scope["changed_lines"], "lenses": [lens["name"] for lens in lenses],
        "gate": [(r["cmd"].split()[-1] if r.get("exit") is not None else r["cmd"], r.get("exit", r.get("skipped")))
                 for r in gate["results"]],
        "contract_surfaces": blast["contract_surfaces_touched"], "reach": blast["reach"],
        "tiers": {p: t["tier"] for p, t in blast["tiers"].items()}, "codemap": blast["codemap"],
        "tests_selected_by": gate.get("tests_selected_by"),
        "packets": {f: len(text.split()) for f, text in packets.items()},
    }, indent=1))


# =========================================================================== report
def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def validate(f: dict[str, Any], warn: list[str]) -> bool:
    missing = [k for k in REQUIRED if k not in f]
    bad = [k for k, ok in (("severity", SEV), ("evidence", EVIDENCE), ("blast", BLAST)) if f.get(k) not in ok]
    if missing or bad:
        warn.append(f"dropped malformed finding {f.get('id', '?')}: missing={missing} invalid={bad}")
        return False
    warn += [f"{f['id']}: '{k}' exceeds 2 sentences"
             for k in ("summary", "failure", "fix") if _sentences(str(f[k])) > 2]
    return True


def merge(reviewers: list[dict[str, Any]], warn: list[str]) -> list[dict[str, Any]]:
    """Dedupe across reviewers. Same id (ignoring the lens prefix) is one finding. Other ids are one only when
    *different* reviewers cite the same file, symbol (non-empty) and severity within MERGE_LINES
    lines: independent reviewers name the same defect with different slugs, but one reviewer's
    two ids are two defects. Keep the strongest evidence (first reviewer wins ties), join lens
    names, record how many reviewers raised it."""
    merged: list[dict[str, Any]] = []
    sources: list[set[int]] = []                         # reviewer indexes behind each merged finding

    def same_defect(old: dict[str, Any], f: dict[str, Any], src: set[int], ri: int) -> bool:
        if old["id"].split(":", 1)[-1] == f["id"].split(":", 1)[-1]:     # same path:symbol:slug, any lens
            return True
        return (ri not in src and bool(f.get("symbol")) and old.get("symbol") == f["symbol"]
                and old["file"] == f["file"] and old["severity"] == f["severity"]
                and abs(int(old.get("line") or 0) - int(f.get("line") or 0)) <= MERGE_LINES)

    def rank(x: dict[str, Any]) -> tuple[int, int]:      # stronger evidence, then an uncorrected line
        return EVIDENCE.index(x["evidence"]), int("line_reported" in x)

    for ri, r in enumerate(reviewers):
        for f in r.get("findings", []):
            if not validate(f, warn):
                continue
            i = next((i for i, old in enumerate(merged) if same_defect(old, f, sources[i], ri)), None)
            if i is None:
                merged.append(dict(f))
                sources.append({ri})
                continue
            old = merged[i]
            best = f if rank(f) < rank(old) else old
            merged[i] = {**best, "lens": "+".join(sorted(set(old["lens"].split("+")) | {f["lens"]})),
                         "raised_by": old.get("raised_by", 1) + 1}
            sources[i].add(ri)
    return merged


REFUTER_VERDICTS = ("kept", "downgraded", "dropped")
WEAKER = (("severity", SEV, 1), ("evidence", EVIDENCE, 1), ("blast", BLAST, -1))   # direction of "weaker"


def apply_refuter(findings: list[dict[str, Any]], verdicts: list[dict[str, Any]] | None,
                  warn: list[str] | None = None) -> tuple[list, dict]:
    """A refuter can only cut. `downgraded` may lower severity, evidence and blast; `kept` may
    weaken evidence or narrow blast, and leaves PROVEN alone (a run that showed the failure
    outranks a refuter that ran nothing, incl. red-team upgrades merged in). Nothing is ever
    raised. An unknown verdict is warned about and the finding stays unrefuted."""
    warn = [] if warn is None else warn
    stats: dict[str, Any] = {"in": 0, "kept": 0, "downgraded": 0, "dropped": 0, "ran": verdicts is not None}
    by_id = {v["id"]: v for v in verdicts or [] if isinstance(v, dict) and "id" in v}
    out = []
    for f in findings:
        v = by_id.get(f["id"])
        if v is not None and v.get("verdict") not in REFUTER_VERDICTS:
            warn.append(f"{f['id']}: unknown refuter verdict {v.get('verdict')!r} — left unrefuted")
            v = None
        if v is None:
            out.append({**f, "refuter": "unrefuted"})
            continue
        verdict = v["verdict"]
        stats["in"] += 1
        if verdict == "dropped":
            stats["dropped"] += 1
            continue
        lower = {k: v[k] for k, scale, worse in WEAKER
                 if (k != "severity" or verdict == "downgraded") and v.get(k) in scale
                 and worse * (scale.index(v[k]) - scale.index(f[k])) > 0
                 and not (verdict == "kept" and k == "evidence" and f[k] == "PROVEN")}
        if verdict == "downgraded" and not lower:
            warn.append(f"{f['id']}: refuter said 'downgraded' but lowered nothing — counted as kept")
            verdict = "kept"
        stats[verdict] += 1
        out.append({**f, **lower, "refuter": verdict})
    return out, stats


def _changed_lines(scope: dict[str, Any]) -> dict[str, set[int]]:
    """New-side line numbers touched by the scope's diff (untracked files: every line). prep stores
    the map in scope.json, so report still works after a PR worktree is gone."""
    out: dict[str, set[int]] = defaultdict(set)
    if scope.get("line_map") is not None:
        out.update({p: set(lines) for p, lines in scope["line_map"].items()})
        return out
    repo = Path(scope["repo"])
    if scope.get("diff_args"):
        current = None
        for line in git(repo, *QUOTE_OFF, *scope["diff_args"], *DIFF_U0).splitlines():
            if line.startswith("+++ "):
                current = diff_new_path(line)
            elif current and (m := NEW_RANGE_RX.match(line)):
                start, count = int(m.group(1)), int(m.group(2) or 1)
                out[current].update(range(start, start + max(count, 1)))
    for f in scope["files"]:
        if f.get("untracked"):
            out[f["path"]].update(range(1, f["added"] + 1))
    return out


def _def_spans(path: Path) -> list[tuple[str, int, int]]:
    try:
        return _def_spans_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def _def_spans_text(text: str) -> list[tuple[str, int, int]]:
    """(qualname, first line, last line) of every def/class in Python source."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[str, int, int]] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                spans.append((prefix + n.name, n.lineno, n.end_lineno or n.lineno))
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return spans


def fix_locations(findings: list[dict[str, Any]], scope: dict[str, Any], warn: list[str]) -> None:
    """Reviewers cite wrong lines (e.g. a diff-relative number). Check each file:line against the
    file and the cited symbol's span; relocate to the first changed line inside the symbol (or
    its start). The original line is kept as `line_reported`."""
    repo = Path(scope["repo"])
    if not repo.is_dir():                                # PR re-run: the worktree is already gone
        warn.append(f"{repo} no longer exists; cited locations not rechecked")
        return
    changed = _changed_lines(scope)
    for f in findings:
        if not isinstance(f.get("file"), str) or not isinstance(f.get("line"), int):
            continue                                     # malformed: validate() reports it during merge
        p = repo / f["file"]
        if not p.is_file():
            warn.append(f"{f.get('id', '?')}: cites {f['file']}, which does not exist")
            continue
        n_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        sym = f.get("symbol") or ""
        span = next(((s, e) for q, s, e in _def_spans(p) if sym and (q == sym or q.endswith("." + sym))), None) \
            if p.suffix == ".py" else None
        line = f["line"]
        if 1 <= line <= n_lines and (span is None or span[0] <= line <= span[1]):
            continue
        inside = sorted(ln for ln in changed.get(f["file"], ()) if span is None or span[0] <= ln <= span[1])
        new = inside[0] if inside else (span[0] if span else None)
        if new is None:
            warn.append(f"{f['id']}: line {line} is outside {f['file']} ({n_lines} lines) and could not be relocated")
            continue
        f["line_reported"], f["line"] = line, new
        where = f"`{sym}` spans {span[0]}-{span[1]}" if span else f"file has {n_lines} lines"
        warn.append(f"{f['id']}: line {line} corrected to {new} ({where})")


def _cell(text: str) -> str:
    """Text safe inside a markdown table cell: a `|` would end the cell, a newline the row."""
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _clip(text: str, sentences: int = 2) -> str:
    """First `sentences` sentences for the terminal report; the JSON keeps the full text."""
    parts = [s for s in re.split(r"(?<=[.!?])\s+", str(text).strip()) if s]
    return " ".join(parts[:sentences]) + (" …" if len(parts) > sentences else "")


def _clip_chars(text: str, limit: int = 180) -> str:
    """Proofs are commands/paths, where '.' isn't a sentence end: clip by length instead."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def _dedupe(items: list[str], cap: int = 5) -> list[str]:
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", " ", it.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out[:cap] + ([f"+{len(out) - cap} more in JSON"] if len(out) > cap else [])


def verdict_of(findings: list[dict[str, Any]]) -> str:
    sevs = {f["severity"] for f in findings}
    return ("BLOCK" if "P0" in sevs else "FIX-FIRST" if "P1" in sevs
            else "SHIP-WITH-NOTES" if findings else "SHIP")


def render(r: dict[str, Any], scope: dict[str, Any], blast: dict[str, Any], for_post: bool = False) -> str:
    f_all, ledger = r["findings"], r["ledger"]
    shown = [f for f in f_all if f["severity"] != "P3"] + [f for f in f_all if f["severity"] == "P3"][:P3_CAP]
    lines = [f"## Review · {scope['target']} @ {scope['head_sha'][:7]} · {r['effort']} · "
             f"verdict: {r['verdict']} (advisory)", f"Intent: {r['intent']}"]
    if scope.get("deep"):
        lines.append(f"Effort: {scope['deep']}")
    lines.append("")
    if shown:
        lines += ["| # | Sev | Evidence | Blast | Where | Finding |",
                  "|---|-----|----------|-------|-------|---------|"]
        lines += [f"| {i} | {f['severity']} | {f['evidence']} | {f['blast']} | "
                  f"[{f['file']}:{f['line']}]({f['file']}#L{f['line']}) | {_cell(_clip(f['summary'], 1))} |"
                  for i, f in enumerate(shown, 1)]
        lines.append("")
        lines += [f"**{i}. {_clip(f['summary'], 1)}**  Failure: {_clip(f['failure'])} Fix: {_clip(f['fix'])}  "
                  f"Proof: {_clip_chars(f['proof'])}"
                  for i, f in enumerate(shown, 1)]
        if len(f_all) > len(shown):
            lines.append(f"{len(f_all) - len(shown)} P3 nits collapsed (see JSON).")
    else:
        lines.append("No findings.")
    executed = [f"`{g['cmd']}` → exit {g['exit']}" + (" (environment)" if g.get("environment") else "")
                for g in ledger["gate"] if g.get("exit") is not None]
    skipped = _dedupe([f"`{g['cmd']}` — {g['skipped']}" for g in ledger["gate"] if g.get("exit") is None]
                      + ledger["skipped"])
    ref = ledger["refuter"]
    ref_line = (f"{ref['in']} in → {ref['kept']} kept, {ref['downgraded']} downgraded, {ref['dropped']} dropped"
                if ref["ran"] else "skipped (no P0–P1)")  # noqa: RUF001 — matches contract wording
    usages = sum(s["usages"] for s in blast["symbols"]) + sum(m["importer_files"] for m in blast["modules"])
    tiers = blast.get("tiers", {})
    by_tier: dict[str, list[str]] = defaultdict(list)
    for p, t in tiers.items():
        by_tier[t["tier"]].append(p)
    touches = " · ".join(f"{t} {len(by_tier[t])}" + (f" ({', '.join(by_tier[t][:3])})" if t == "TRUNK" else "")
                         for t in ("TRUNK", "BRANCH", "LEAF", "UNMAPPED") if by_tier.get(t))
    cm = blast.get("codemap", {})
    lines += ["", "### Validation ledger",
              f"- Lenses: {', '.join(ledger['lenses'])} (reviewers: {ledger['reviewers']}, "
              f"fresh context: {'no' if r['effort'] == 'quick' else 'yes'})",
              f"- Read: {len(scope['files'])} changed files; blast map: {len(blast['symbols'])} symbols, {usages} "
              f"callers/importers, measured reach: {blast.get('reach', 'LOCAL')}, "
              f"contract surfaces: {', '.join(blast['contract_surfaces_touched']) or 'none'}",
              f"- Touches: {touches or 'n/a'}" + (f" — {cm['note']}" if cm.get("note") else ""),
              f"- Checked: {len(ledger['checked'])} items (see JSON)",
              f"- Executed: {'; '.join(executed) or 'none'}",
              f"- Skipped: {'; '.join(skipped) or 'none'}",
              f"- Refuter: {ref_line} · Suppressed: {ledger['suppressed']}"]
    if r["effort"] == "deep":
        impact = ledger.get("impact", [])
        lines.append(f"- Impact reviewers: {len(impact)}"
                     + (f" ({', '.join(t['subsystem'] for t in impact)})" if impact
                        else " (no cross-subsystem consumer in the caller tree)"))
        if diff_line := _diff_tests_line(ledger.get("diff_tests")):
            lines.append(f"- {diff_line}")
        proofs = ledger.get("proofs", [])
        where = f" in `{Path(proofs[0]).parent}`" if proofs and not for_post else ""
        lines.append("- Proof tests (proposed regression tests)" + where + ": "
                     + (", ".join(f"`{Path(p).name}`" for p in proofs) if proofs else "none"))
    if cost := _cost_line(ledger):
        lines.append(f"- {cost}")
    if not for_post:                                     # local paths never go to the PR
        lines.append(f"- Report: {r['report_path']}")
    if r["effort"] in ("quick", "standard") and (by_tier.get("TRUNK") or blast.get("reach") == "CONTRACT"):
        why = "TRUNK touched" if by_tier.get("TRUNK") else "reach CONTRACT"
        lines.append(f"\n{why} — consider `deep` effort before merging.")
    return "\n".join(lines)


def cmd_report(a: argparse.Namespace) -> None:
    work = Path(a.work)
    scope, lenses = load_json(work / "scope.json"), load_json(work / "lenses.json")
    blast = load_json(work / "blast.json")
    gate = load_json(work / "gate.json") if (work / "gate.json").exists() else {"results": []}
    repo = Path(scope["repo"])
    warn: list[str] = []
    reviewers: list[dict[str, Any]] = []
    for path in a.findings:
        data = load_json(path)
        reviewers += data if isinstance(data, list) else [data]
    fix_locations([f for r in reviewers for f in r.get("findings", [])], scope, warn)  # before merge
    findings = merge(reviewers, warn)
    suppressed_ids = set(load_suppressed_ids(Path(scope.get("origin_repo", repo))))   # PR: never the PR's pack
    suppressed = sum(1 for f in findings if f["id"] in suppressed_ids)
    findings, ref_stats = apply_refuter([f for f in findings if f["id"] not in suppressed_ids],
                                        load_json(a.refuter) if a.refuter else None, warn)
    findings = [f for f in findings if not (f["severity"] == "P3" and f["evidence"] == "INFERRED")]
    findings.sort(key=lambda f: (SEV.index(f["severity"]), EVIDENCE.index(f["evidence"])))
    gone = not repo.is_dir()                             # PR re-run: read the reviewed head from the checkout
    origin = Path(scope.get("origin_repo", repo))
    for f in findings:                                   # what `status` compares the code against later
        text = git(origin, "show", f"{scope['head_sha']}:{f['file']}", check=False) if gone else None
        if fp := code_fingerprint(repo / f["file"], f, text or None):
            f["code_fp"] = fp

    # Default to the reviewed repo, pack or not, so the authoring session finds the report at a
    # repo-relative path instead of in another session's scratchpad. PR reviews use the user's
    # checkout, which outlives the temp worktree.
    out_dir = Path(a.out_dir) if a.out_dir else Path(scope.get("origin_repo", repo)) / PACK_REL / "out"
    ensure_ignored(out_dir)
    now = dt.datetime.now(dt.UTC)
    report_path = out_dir / f"{now:%Y%m%dT%H%M}-{scope['head_sha'][:7]}.json"
    import history
    effort = scope.get("effort", "standard")
    kind = a.kind or ("author-quick" if effort == "quick" else "independent")
    if not a.cost and kind != "test":
        warn.append("no --cost given: the run is recorded with cost_missing (pass each agent's subagent_tokens)")
    report = {
        "schema": history.SCHEMA, "repo": history.repo_key(origin), "target": scope["target"],
        "head_sha": scope["head_sha"], "effort": effort, "mode": "advisory", "verdict": verdict_of(findings),
        "intent": scope.get("intent", ""), "kind": kind,
        "ledger": {"lenses": [lens["name"] for lens in lenses], "reviewers": len(reviewers), "gate": gate["results"],
                   "blast_map": blast, "checked": [c for r in reviewers for c in r.get("checked", [])],
                   "skipped": a.skipped + [c for r in reviewers for c in r.get("not_checked", [])],
                   "refuter": ref_stats, "suppressed": suppressed, "deep": scope.get("deep"),
                   "impact": load_json(work / "impact.json") if (work / "impact.json").exists() else [],
                   "diff_tests": load_json(work / "diff_tests.json") if (work / "diff_tests.json").exists() else None,
                   "proofs": sorted(str(p) for p in Path(a.proofs_dir or work / "proofs").glob("test_*.py")),
                   "tokens": _parse_cost(a.cost), "wall_s": _wall_seconds(work, now)},
        "findings": findings, "warnings": warn, "report_path": str(report_path),
    }
    if not a.no_history:
        inputs = [Path(p).read_text(encoding="utf-8") for p in a.findings]

        def read_code(path: str) -> str | None:              # the code as this run reviewed it
            if repo.is_dir() and (repo / path).is_file():
                return (repo / path).read_text(encoding="utf-8", errors="replace")
            return history._show(origin, scope["head_sha"], path)
        try:                                             # recording must never cost the review its report
            run = history.record_run(report, scope, lenses, reviewer_inputs=inputs, kind=kind, model=a.model,
                                     review_wall_s=report["ledger"]["wall_s"], read_code=read_code)
            report["run_id"] = run["run_id"]
        except Exception as exc:                         # noqa: BLE001 - reported loudly, not swallowed
            warn.append(f"review history NOT recorded ({type(exc).__name__}: {exc}). Fix the history "
                        f"file, then re-run this `report` (same inputs update the same run)")
    dump_json(report, report_path)
    dump_json(report, out_dir / "latest.json")
    with (out_dir / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": now.isoformat(timespec="minutes"), "run_id": report.get("run_id"), "kind": kind,
            "target": scope["target"], "head_sha": scope["head_sha"],
            "effort": report["effort"], "verdict": report["verdict"],
            "counts": {s: sum(f["severity"] == s for f in findings) for s in SEV},
            "evidence": {e: sum(f["evidence"] == e for f in findings) for e in EVIDENCE}, "refuter": ref_stats,
            "per_lens": {n: sum(n in f["lens"].split("+") for f in findings) for n in report["ledger"]["lenses"]},
            "tokens": report["ledger"]["tokens"], "wall_s": report["ledger"]["wall_s"],
        }) + "\n")
    print(render(report, scope, blast))
    for w in warn:
        print(f"warning: {w}", file=sys.stderr)
    if a.post or a.post_dry_run:
        try:
            actions = post_review(report, scope, blast, dry_run=a.post_dry_run, pr_number=a.pr_number,
                                  title_prefix=a.title_prefix, allow_stale=a.allow_stale)
            print("\n### Posted to PR" + (" (dry run — nothing sent)" if a.post_dry_run else ""))
            print("\n".join(f"- {x}" for x in actions))
        except PostError as exc:
            print(f"\nposting skipped: {exc}", file=sys.stderr)
    if scope.get("temp_worktree") and not a.keep_worktree:
        remove_pr_worktree(Path(scope["origin_repo"]), Path(scope["temp_worktree"]), scope.get("links", []))


# =========================================================================== cost
def _parse_cost(specs: list[str] | None) -> dict[str, int]:
    """`--cost "reviewer=50163,refuter=38328"` (repeatable) -> per-agent tokens + total."""
    out: dict[str, int] = {}
    for spec in specs or []:
        for part in spec.split(","):
            if "=" in part:
                name, _, n = part.partition("=")
                if n.strip().isdigit():
                    out[name.strip()] = out.get(name.strip(), 0) + int(n.strip())
    if out:
        out["total"] = sum(out.values())
    return out


WALL_CAP_S = 6 * 3600          # longer means the review was left idle, not that it took that long


def _wall_seconds(work: Path, now: dt.datetime) -> int | None:
    """Seconds from `review.py start` (the first reviewer spawn) to `report`. None when no start was
    marked, or when the gap exceeds WALL_CAP_S (an idle session would otherwise read as a long review)."""
    marker = work / "started_at"
    if not marker.exists():
        return None
    secs = int((now - dt.datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())).total_seconds())
    return secs if 0 <= secs <= WALL_CAP_S else None


def _cost_line(ledger: dict[str, Any]) -> str | None:
    tokens, wall = ledger.get("tokens") or {}, ledger.get("wall_s")
    if not tokens and wall is None:
        return None
    agents = len([k for k in tokens if k != "total"])
    parts = [f"{tokens['total'] / 1000:.0f}k tokens ({agents} agent{'s' if agents != 1 else ''})"] if tokens else []
    if wall is not None:
        parts.append(f"{wall // 60}m {wall % 60:02d}s review time")
    return "Cost: " + " · ".join(parts)


# =========================================================================== posting (GitHub)
SUMMARY_MARK = "<!-- review-core:summary -->"
ID_MARK_RX = re.compile(r"<!-- review-core:id=(\S+?) -->")
STATE_MARK_RX = re.compile(r"<!-- review-core:state=(gone|back) -->")   # on our replies to an inline finding
# GitHub's answers when the token may comment but not (un)resolve threads (no repo write access).
PERMISSION_DENIED_RX = re.compile(r"FORBIDDEN|not accessible by|HTTP 403", re.IGNORECASE)
VERDICT_ICON = {"BLOCK": "🔴", "FIX-FIRST": "🟠", "SHIP-WITH-NOTES": "🟡", "SHIP": "🟢"}
REVIEW_LABELS = {
    "review:trunk": ("b60205", "Touches a TRUNK module (review-core code map)"),
    "review:contract": ("d93f0b", "Blast reach CONTRACT (review-core)"),
    "review:deep": ("5319e7", "Reviewed at deep effort (review-core)"),
    "ai-verdict:block": ("b60205", "review-core verdict: BLOCK (advisory)"),
    "ai-verdict:fix-first": ("d93f0b", "review-core verdict: FIX-FIRST (advisory)"),
    "ai-verdict:ship-with-notes": ("fbca04", "review-core verdict: SHIP-WITH-NOTES (advisory)"),
    "ai-verdict:ship": ("0e8a16", "review-core verdict: SHIP (advisory)"),
}
TITLE_TAG_RX = re.compile(r"^(?:\[(?:TRUNK|CONTRACT|BLOCK|FIX-FIRST|SHIP-WITH-NOTES|SHIP)\])+\s*")


class PostError(RuntimeError):
    """Posting could not proceed (no PR, stale head, unsupported provider, gh failure)."""


def find_gh() -> str:
    import shutil
    for cand in (shutil.which("gh"), r"C:\Program Files\GitHub CLI\gh.exe"):
        if cand and Path(cand).exists():
            return cand
    raise PostError("GitHub CLI `gh` not found (install it and run `gh auth login`)")


class GitHubPoster:
    """`gh api` over an argument list; request bodies travel on stdin, never on argv."""

    def __init__(self, cwd: Path, dry_run: bool = False) -> None:
        self.cwd, self.dry_run, self.gh = cwd, dry_run, find_gh()
        self.sent: list[tuple[str, str]] = []

    def api(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.dry_run and method != "GET":
            self.sent.append((method, path))
            return {}
        args = [self.gh, "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if method == "GET":
            args += ["--paginate", "--slurp"]
        if payload is not None:
            args += ["--input", "-"]
        proc = subprocess.run(args, cwd=self.cwd, input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise PostError(f"gh api {method} {path}: {(proc.stderr + ' ' + proc.stdout).strip()[:500]}")
        self.sent.append((method, path))
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        if method == "GET" and isinstance(data, list) and all(isinstance(p, list) for p in data):
            data = [item for page in data for item in page]        # --slurp: list of pages
        elif method == "GET" and isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        return data

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        return self.api("POST", "graphql", {"query": query, "variables": variables})


def _inline_body(f: dict[str, Any]) -> str:
    return (f"**{f['severity']} · {f['evidence']} · {f['blast']}** {_clip(f['summary'], 1)}\n\n"
            f"**Failure:** {_clip(f['failure'])}\n**Fix:** {_clip(f['fix'])}\n**Proof:** {_clip_chars(f['proof'])}\n\n"
            f"<sub>review-core · advisory · lens `{f['lens']}`</sub>\n<!-- review-core:id={f['id']} -->")


def summary_body(report: dict[str, Any], scope: dict[str, Any], blast: dict[str, Any],
                 off_diff: list[dict[str, Any]]) -> str:
    tiers = {t["tier"] for t in blast.get("tiers", {}).values()}
    trunk = [p for p, t in blast.get("tiers", {}).items() if t["tier"] == "TRUNK"]
    head = [f"{VERDICT_ICON.get(report['verdict'], '')} **{report['verdict']}** (advisory)"]
    if "TRUNK" in tiers:
        head.append(f"TRUNK touched ({', '.join(f'`{p}`' for p in trunk[:2])})")
    head += [f"reach **{blast.get('reach', 'LOCAL')}**", report["effort"]]
    body = render(report, scope, blast, for_post=True)
    findings_part, _, ledger_part = body.partition("### Validation ledger")
    findings_part = "\n".join(findings_part.splitlines()[1:]).strip()          # drop the terminal header
    extra = ""
    if off_diff:
        extra = "\n\n_Not on a diff line, so reported here only:_ " + "; ".join(
            f"`{f['file']}:{f['line']}` ({f['severity']})" for f in off_diff)
    return (f"{SUMMARY_MARK}\n### review-core\n{' · '.join(head)}\n\n{findings_part}{extra}\n\n"
            f"<details><summary>Validation ledger</summary>\n\n{ledger_part.strip()}\n\n</details>\n\n"
            f"<sub>Reviewed head `{report['head_sha'][:7]}` · advisory only — a human approves and merges.</sub>")


def wanted_labels(report: dict[str, Any], blast: dict[str, Any]) -> set[str]:
    labels = {f"ai-verdict:{report['verdict'].lower()}"}
    if any(t["tier"] == "TRUNK" for t in blast.get("tiers", {}).values()):
        labels.add("review:trunk")
    if blast.get("reach") == "CONTRACT":
        labels.add("review:contract")
    if report["effort"] == "deep":
        labels.add("review:deep")
    return labels


def _resolve_pr(poster: GitHubPoster, scope: dict[str, Any], pr_number: int | None) -> tuple[str, int, dict[str, Any]]:
    origin = Path(scope.get("origin_repo", scope["repo"]))
    if (scope.get("pr") or {}).get("kind") == "gitlab":
        raise PostError("posting to GitLab merge requests is not supported yet (GitHub only)")
    host, project = origin_project(origin)
    if not project or (host and "github" not in host):
        raise PostError(f"origin is not a GitHub repo ({host or 'unknown host'}); GitHub only for now")
    number = pr_number or (scope.get("pr") or {}).get("number")
    if number is None:
        view = subprocess.run([poster.gh, "pr", "view", "--json", "number"], cwd=origin,
                              capture_output=True, text=True, encoding="utf-8")
        if view.returncode:
            raise PostError("no PR for this branch — open one first, or pass --pr-number")
        number = json.loads(view.stdout)["number"]
    pr = poster.api("GET", f"repos/{project}/pulls/{number}")
    return project, int(number), pr


def post_review(report: dict[str, Any], scope: dict[str, Any], blast: dict[str, Any], *, dry_run: bool = False,
                pr_number: int | None = None, title_prefix: bool = False, allow_stale: bool = False,
                poster: Any = None) -> list[str]:
    """Create/update the summary comment, inline P0/P1 comments, labels and (optionally) a title
    prefix. Idempotent: re-running edits in place and resolves threads for findings now gone."""
    poster = poster or GitHubPoster(Path(scope.get("origin_repo", scope["repo"])), dry_run=dry_run)
    project, n, pr = _resolve_pr(poster, scope, pr_number)
    head = pr.get("head", {}).get("sha", "")
    if head and head != report["head_sha"] and not allow_stale:
        raise PostError(f"PR head is {head[:7]} but the review covers {report['head_sha'][:7]} — "
                        "re-run the review (or pass --allow-stale)")
    actions: list[str] = []
    changed = _changed_lines(scope)
    blocking = [f for f in report["findings"] if f["severity"] in ("P0", "P1")]
    on_diff = [f for f in blocking if f["line"] in changed.get(f["file"], set())]
    off_diff = [f for f in blocking if f not in on_diff]

    # summary comment: edit in place
    body = summary_body(report, scope, blast, off_diff)
    existing = [c for c in poster.api("GET", f"repos/{project}/issues/{n}/comments") or []
                if SUMMARY_MARK in (c.get("body") or "")]
    if existing:
        poster.api("PATCH", f"repos/{project}/issues/comments/{existing[0]['id']}", {"body": body})
        actions.append(f"summary comment updated ({report['verdict']})")
    else:
        poster.api("POST", f"repos/{project}/issues/{n}/comments", {"body": body})
        actions.append(f"summary comment created ({report['verdict']})")

    # inline comments: new findings posted; findings now gone replied-to and resolved once;
    # a gone finding that is reported again gets its thread reopened
    posted: dict[str, dict[str, Any]] = {}
    state: dict[int, str] = {}                           # root comment id -> last state we replied with
    comments = poster.api("GET", f"repos/{project}/pulls/{n}/comments") or []
    for c in comments:
        if (m := ID_MARK_RX.search(c.get("body") or "")) and not c.get("in_reply_to_id"):
            posted[m.group(1)] = c
    for c in sorted(comments, key=lambda c: c["id"]):
        if c.get("in_reply_to_id") and (m := STATE_MARK_RX.search(c.get("body") or "")):
            state[c["in_reply_to_id"]] = m.group(1)
    current = {f["id"] for f in on_diff}
    new = [f for f in on_diff if f["id"] not in posted]
    if new:
        poster.api("POST", f"repos/{project}/pulls/{n}/reviews", {
            "commit_id": report["head_sha"], "event": "COMMENT",
            "body": f"review-core: {len(new)} inline finding(s) — see the summary comment.",
            "comments": [{"path": f["file"], "line": f["line"], "side": "RIGHT", "body": _inline_body(f)}
                         for f in new]})
        actions.append(f"{len(new)} inline comment(s) posted (P0/P1 on diff lines)")
    gone = [(fid, c) for fid, c in posted.items() if fid not in current and state.get(c["id"]) != "gone"]
    back = [(fid, c) for fid, c in posted.items() if fid in current and state.get(c["id"]) == "gone"]
    threads = _thread_ids(poster, project, n) if gone or back else {}
    sha = report["head_sha"][:7]
    denied: set[str] = set()                             # refused for lack of repo write access
    errors: dict[str, str] = {}                          # failed for any other reason: keep the error
    for fid, c, text, mutation, act_if_resolved in (
            *((fid, c, f"✅ no longer reported at `{sha}`", "resolveReviewThread", False) for fid, c in gone),
            *((fid, c, f"⚠️ reported again at `{sha}`", "unresolveReviewThread", True) for fid, c in back)):
        mark = "gone" if mutation == "resolveReviewThread" else "back"
        poster.api("POST", f"repos/{project}/pulls/{n}/comments/{c['id']}/replies",
                   {"body": f"{text}\n<!-- review-core:state={mark} -->"})
        tid, resolved = threads.get(fid, (None, None))
        if tid is not None and resolved == act_if_resolved:
            try:
                poster.graphql(f"mutation($id:ID!){{{mutation}(input:{{threadId:$id}}){{thread{{id}}}}}}",
                               {"id": tid})
            except PostError as exc:
                if PERMISSION_DENIED_RX.search(str(exc)):
                    denied.add(fid)
                else:
                    errors[fid] = str(exc)

    # A token without repository write access (e.g. Contents: read-only) can reply but not
    # (un)resolve threads; say so once, plainly, instead of claiming they were resolved. Any
    # other failure (network, outage) is reported with its own error, not blamed on the token.
    def _thread_note(items: list[tuple[str, Any]], done: str, verb: str, base: str) -> None:
        if not items:
            return
        no_access = sum(fid in denied for fid, _ in items)
        broken = [fid for fid, _ in items if fid in errors]
        ok = len(items) - no_access - len(broken)
        msg = f"{len(items)} {done}: replied"
        if ok:
            msg += f", {ok} thread(s) {verb}"
        if no_access:
            msg += f"; {no_access} thread(s) not {verb}: the token lacks repo write access, so {base} them on GitHub"
        actions.append(msg)
        actions.extend(f"could not {base} thread for {fid}: {_clip_chars(errors[fid])}" for fid in broken)
    _thread_note(gone, "earlier inline finding(s) no longer reported", "resolved", "resolve")
    _thread_note(back, "resolved inline finding(s) reported again", "reopened", "reopen")
    if off_diff:
        actions.append(f"{len(off_diff)} P0/P1 not on a diff line — in the summary only")

    # labels: ensure they exist, then sync ours
    want = wanted_labels(report, blast)
    for name in want:
        color, desc = REVIEW_LABELS[name]
        try:
            poster.api("POST", f"repos/{project}/labels", {"name": name, "color": color, "description": desc})
        except PostError as exc:
            if "already_exists" not in str(exc):
                raise
    have = {lb["name"] for lb in pr.get("labels", [])}
    from urllib.parse import quote
    for stale_label in sorted((have & set(REVIEW_LABELS)) - want):
        poster.api("DELETE", f"repos/{project}/issues/{n}/labels/{quote(stale_label, safe='')}")
    if want - have:
        poster.api("POST", f"repos/{project}/issues/{n}/labels", {"labels": sorted(want - have)})
    actions.append(f"labels: {', '.join(sorted(want))}")

    if title_prefix:
        tags = ([f"[{t}]" for t in ("TRUNK",) if "review:trunk" in want]
                + (["[CONTRACT]"] if "review:contract" in want and "review:trunk" not in want else [])
                + [f"[{report['verdict']}]"])
        title = TITLE_TAG_RX.sub("", pr.get("title", ""))
        new_title = f"{''.join(tags)} {title}"
        if new_title != pr.get("title"):
            poster.api("PATCH", f"repos/{project}/pulls/{n}", {"title": new_title})
            actions.append(f"title: {new_title}")
    return actions


def _thread_ids(poster: Any, project: str, n: int) -> dict[str, tuple[str, bool]]:
    """finding id -> (review thread id, is resolved) via GraphQL, to resolve or reopen threads."""
    owner, name = project.split("/", 1)
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
         "reviewThreads(first:100){nodes{id isResolved comments(first:1){nodes{body}}}}}}}")
    try:
        data = poster.graphql(q, {"o": owner, "r": name, "n": n})
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (PostError, KeyError, TypeError):
        return {}
    out = {}
    for t in nodes:
        body = (t["comments"]["nodes"] or [{}])[0].get("body", "")
        if m := ID_MARK_RX.search(body):
            out[m.group(1)] = (t["id"], bool(t["isResolved"]))
    return out


# =========================================================================== status
def code_fingerprint(path: Path, f: dict[str, Any], text: str | None = None) -> str | None:
    """Hash of the code a finding cites: its symbol's lines (Python), else the cited line ± 2.
    `report` stores it as `code_fp` from the code as reviewed (working tree or PR worktree), so
    `status` compares like with like instead of diffing against a commit. `text` overrides
    reading `path` (a PR re-run reads the committed head instead)."""
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    lines, sym = text.splitlines(), f.get("symbol")
    span = next(((s, e) for q, s, e in _def_spans_text(text) if q == sym or q.endswith(f".{sym}")), None) \
        if path.suffix == ".py" and sym else None
    if span is None:
        line = int(f.get("line") or 0)
        if not 1 <= line <= len(lines):
            return None
        span = (max(line - 2, 1), min(line + 2, len(lines)))
    body = "\n".join(ln.rstrip() for ln in lines[span[0] - 1:span[1]])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def finding_status(repo: Path, report: dict[str, Any], suppressions: dict[str, str]) -> list[dict[str, Any]]:
    """Each finding of a saved report as suppressed / open / changed / unknown. This reads the
    code, it doesn't re-review: "changed" means the cited code differs from what was reviewed,
    so recheck; only a fresh review says a finding is fixed. A difference only counts as an edit
    when this checkout contains the reviewed commit; otherwise (a PR you haven't checked out,
    another branch) the status is unknown."""
    sha = report.get("head_sha", "")
    contains = bool(sha) and subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "HEAD"],
                                            capture_output=True).returncode == 0
    rows = []
    for i, f in enumerate(report["findings"], 1):
        now = code_fingerprint(repo / f["file"], f)
        if f["id"] in suppressions:
            status, note = "suppressed", suppressions[f["id"]]
        elif not f.get("code_fp"):
            status, note = "unknown", "report has no code fingerprint; re-run the review"
        elif now == f["code_fp"]:
            status, note = "open", ""
        elif not contains:
            status, note = "unknown", f"this checkout doesn't contain the reviewed commit {sha[:7]}"
        elif now is None:
            status, note = "changed", "cited file or code removed since review"
        else:
            status, note = "changed", f"`{f.get('symbol') or 'cited lines'}` edited since review"
        rows.append({"n": i, "status": status, "note": note, "finding": f})
    return rows


def render_status(report: dict[str, Any], rows: list[dict[str, Any]], report_path: Path, show_all: bool) -> str:
    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")
    count = Counter(r["status"] for r in rows)
    lines = [f"## Review findings · {report['target']} @ {report['head_sha'][:7]} · verdict {report['verdict']}",
             f"{count['open']} open · {count['changed']} changed since review (recheck) · "
             f"{count['suppressed']} suppressed" + (f" · {count['unknown']} unknown" if count["unknown"] else ""), ""]
    shown = [r for r in rows if show_all or r["status"] != "suppressed"]
    if shown:
        label = {"open": "open", "changed": "⚠ changed — recheck", "suppressed": "suppressed", "unknown": "? unknown"}
        lines += ["| # | Sev | Evidence | Where | Status | Problem | Fix |",
                  "|---|-----|----------|-------|--------|---------|-----|"]
        for r in shown:
            f = r["finding"]
            status = label[r["status"]] + (f" ({r['note']})" if r["note"] else "")
            lines.append(f"| {r['n']} | {f['severity']} | {f['evidence']} | [{f['file']}:{f['line']}]"
                         f"({f['file']}#L{f['line']}) | {cell(status)} | {cell(_clip(f['summary'], 1))} | "
                         f"{cell(_clip(f['fix'], 1))} |")
    else:
        lines.append("No findings to show.")
    lines += ["", "Numbers match the review's table. Status reads the code since the review; only a fresh "
              "review marks a finding fixed.", f"Report: {report_path}"]
    return "\n".join(lines)


def cmd_status(a: argparse.Namespace) -> None:
    repo = Path(a.repo).resolve()
    path = Path(a.report) if a.report else repo / PACK_REL / "out" / "latest.json"
    if not path.exists():
        raise SystemExit(f"no review report at {path}: run a review first, or pass --report")
    report = load_json(path)
    print(render_status(report, finding_status(repo, report, load_suppressions(repo)), path, a.all))


# =========================================================================== history commands
def cmd_start(a: argparse.Namespace) -> None:
    """Mark the review start (call right before spawning the first reviewer) for review_wall_s."""
    (Path(a.work) / "started_at").write_text(dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), encoding="utf-8")


def cmd_label(a: argparse.Namespace) -> None:
    import history
    ev = history.label(Path(a.repo).resolve(), a.finding, a.outcome, a.reason, by=a.by)
    print(f"{ev['id']}: {ev['outcome']} ({ev['source']})")


def _merge_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| # | Sev | Where | State at merge | Suggested | Finding |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        f = r["finding"]
        lines.append(f"| {i} | {f['severity']} | {f['file']}:{f['line']} | {r['state']} | "
                     f"{r['suggest'] or 'needs a label'} | {_cell(_clip(f['summary'], 1))} |")
    return "\n".join(lines)


def cmd_outcomes(a: argparse.Namespace) -> None:
    """At merge: findings of the PR's last review still without an outcome, against the merged code."""
    import history
    repo = Path(a.repo).resolve()
    rows = history.merge_outcomes(repo, a.pr)
    if not rows:
        print(f"PR {a.pr}: nothing to label (no recorded review, or every finding has an outcome).")
    else:
        print(f"## PR {a.pr} · findings needing an outcome\n\n{_merge_table(rows)}\n")
        if a.accept_suggested:
            n = history.accept_suggestions(repo, rows, a.reason or "accepted at merge")
            print(f"Recorded {n} suggested outcome(s). Label the rest with `review.py label`.")
        else:
            print("Accept the suggestions with `--accept-suggested`; label the rest with "
                  "`review.py label --repo <repo> <finding> <outcome> \"<reason>\"`.")
    last = max((r.get("at", "") for r in history.load(history.repo_key(repo))["retro"]), default="")
    if not last or (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(last)).days > 30:
        print(f"\nLast retro: {last[:10] or 'never'}. Run the monthly retro: `review.py retro --repo {a.repo}`.")


def cmd_escape(a: argparse.Namespace) -> None:
    import history
    repo = Path(a.repo).resolve()
    if a.fix:
        # --pr overrides the blamed PR; class and covering findings are computed for that PR
        cand = history.attribute(repo, a.fix, intro_pr=a.pr)
        if cand is None:
            raise SystemExit("nothing to attribute: no blamable lines, or the bug was fixed inside its own PR")
    else:
        if not (a.file and a.note):
            raise SystemExit("give --fix <commit>, or --file and --note (with --symbol and --pr when known)")
        cand = history.manual_escape(repo, a.file, a.symbol or "", a.note, a.pr)
    print(json.dumps({k: v for k, v in cand.items() if k != "blamed"}, indent=2))
    if a.confirm:
        history.confirm_escape(repo, cand, note=a.note or "", klass=a.klass)
        print(f"Recorded escape {cand['fix'][:12]} as {a.klass or cand['class']}.")
    else:
        print("Not recorded. Re-run with --confirm once a human agrees.")


def cmd_retro(a: argparse.Namespace) -> None:
    import history
    repo = Path(a.repo).resolve()
    r = history.retro(repo, month=a.month, since=a.since)
    print(history.render_retro(r))
    if not a.no_compact:
        c = history.compact(history.repo_key(repo))
        pruned = history.prune_reports(repo)
        print(f"\nCompacted months: {', '.join(c['archived']) or 'none'} · expired: {', '.join(c['expired']) or 'none'}"
              f" · local reports pruned: {pruned}")


def cmd_compact(a: argparse.Namespace) -> None:
    import history
    for name in [a.repo_name] if a.repo_name else history.repos():
        c = history.compact(name)
        print(f"{name}: archived {c['archived'] or 'none'}, expired {c['expired'] or 'none'}")


def cmd_precision(a: argparse.Namespace) -> None:
    import history
    sev = set(a.severity.split(",")) if a.severity else None
    months = None if a.window == "all" else int(a.window.rstrip("m"))
    rows = history.precision(a.repo_name, a.lens, sev, months=months, since_lens_change=a.since_lens_change)
    scope = (f"{a.repo_name or 'all repos'} · {a.lens or 'all lenses'} · "
             f"{'since lens change' if a.since_lens_change else a.window}")
    print(history.render_precision(rows, scope))


def cmd_history_import(a: argparse.Namespace) -> None:
    import history
    repo = Path(a.repo).resolve()
    out = repo / PACK_REL / "out"
    paths = [Path(p) for p in a.report] if a.report else [p for p in out.glob("2*.json")]
    print(json.dumps(history.import_reports(repo, paths, set(a.test or [])), indent=2))


# =========================================================================== cli
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--repo", default=".")
    p.add_argument("--target")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--pr", help="PR/MR URL (GitHub /pull/N, GitLab /-/merge_requests/N) or number")
    p.add_argument("--base", help="PR base branch (default: origin default branch)")
    p.add_argument("--intent", required=True)
    p.add_argument("--effort", choices=list(PROOF_BUDGET), default="standard")
    p.add_argument("--work", required=True)
    p.add_argument("--no-gate", action="store_true")
    p.add_argument("--run-pr-code", action="store_true",
                   help="PR reviews: run the gate and diff tests on the PR checkout (executes its code)")
    p.add_argument("--force-deep", action="store_true", help="skip the deep qualification gate")
    p.set_defaults(func=cmd_prep)
    r = sub.add_parser("report")
    r.add_argument("--work", required=True)
    r.add_argument("--findings", action="append", required=True)
    r.add_argument("--refuter")
    r.add_argument("--skipped", action="append", default=[])
    r.add_argument("--out-dir")
    r.add_argument("--proofs-dir", help="red-team proof tests (default <work>/proofs)")
    r.add_argument("--keep-worktree", action="store_true", help="PR reviews: keep the temp worktree")
    r.add_argument("--cost", action="append", help="agent tokens, e.g. \"reviewer=50163,refuter=38328\"")
    r.add_argument("--post", action="store_true", help="post the review to the GitHub PR (only when asked)")
    r.add_argument("--post-dry-run", action="store_true", help="show what --post would send; send nothing")
    r.add_argument("--pr-number", type=int, help="PR to post to (default: --pr target or the branch PR)")
    r.add_argument("--title-prefix", action="store_true", help="prefix the PR title with [TRUNK][VERDICT]")
    r.add_argument("--allow-stale", action="store_true", help="post even if the PR head moved since review")
    r.add_argument("--kind", choices=["independent", "author-quick", "test"],
                   help="default: author-quick for quick effort, else independent; test runs never count")
    r.add_argument("--model", default="sonnet", help="model the reviewer agents ran on")
    r.add_argument("--no-history", action="store_true", help="don't record the run in review-history/")
    r.set_defaults(func=cmd_report)
    st = sub.add_parser("start", help="mark the review start (before spawning the first reviewer)")
    st.add_argument("--work", required=True)
    st.set_defaults(func=cmd_start)
    lb = sub.add_parser("label", help="record a finding's outcome (human; the author may only mark 'disputed')")
    lb.add_argument("--repo", default=".")
    lb.add_argument("--by", choices=["human", "author"], default="human")
    lb.add_argument("finding", help="finding id, key, or a unique suffix")
    lb.add_argument("outcome")
    lb.add_argument("reason")
    lb.set_defaults(func=cmd_label)
    oc = sub.add_parser("outcomes", help="at merge: findings still needing an outcome, against the merged code")
    oc.add_argument("--repo", default=".")
    oc.add_argument("--pr", type=int, required=True)
    oc.add_argument("--accept-suggested", action="store_true", help="record the suggested outcomes (human decision)")
    oc.add_argument("--reason")
    oc.set_defaults(func=cmd_outcomes)
    es = sub.add_parser("escape", help="attribute a bug fix to the PR that introduced the bug")
    es.add_argument("--repo", default=".")
    es.add_argument("--fix", help="the fixing commit")
    es.add_argument("--file")
    es.add_argument("--symbol")
    es.add_argument("--pr", type=int, help="introducing PR (override, or for --file escapes)")
    es.add_argument("--note")
    es.add_argument("--confirm", action="store_true", help="record it (a human has agreed)")
    es.add_argument("--class", dest="klass", help="final class (required when the suggestion is provisional); "
                    "not-a-bug rejects the candidate")
    es.set_defaults(func=cmd_escape)
    rt = sub.add_parser("retro", help="monthly retrospective: merged PRs, bug attribution, then compaction")
    rt.add_argument("--repo", default=".")
    rt.add_argument("--month", help="yyyy-mm (default: the last 30 days)")
    rt.add_argument("--since", help="yyyy-mm-dd")
    rt.add_argument("--no-compact", action="store_true")
    rt.set_defaults(func=cmd_retro)
    cp = sub.add_parser("compact", help="archive closed months, expire old detail, refresh aggregates")
    cp.add_argument("--repo-name", help="history repo name (default: all)")
    cp.set_defaults(func=cmd_compact)
    pc = sub.add_parser("precision", help="per-lens precision with sample size and Wilson lower bound")
    pc.add_argument("--repo-name")
    pc.add_argument("--lens")
    pc.add_argument("--severity", help="e.g. P0,P1")
    pc.add_argument("--window", default="12m", help="12m (default), 6m, … or all")
    pc.add_argument("--since-lens-change", action="store_true")
    pc.set_defaults(func=cmd_precision)
    hi = sub.add_parser("history-import", help="backfill review-history from existing report JSONs")
    hi.add_argument("--repo", default=".")
    hi.add_argument("--report", action="append", help="report JSON(s) (default: the repo's out/ folder)")
    hi.add_argument("--test", action="append", help="report file name(s) to mark as test runs")
    hi.set_defaults(func=cmd_history_import)
    s = sub.add_parser("status", help="table of a saved review's findings and whether their code changed since")
    s.add_argument("--repo", default=".")
    s.add_argument("--report", help="report JSON (default <repo>/.claude/review/out/latest.json)")
    s.add_argument("--all", action="store_true", help="also list suppressed findings")
    s.set_defaults(func=cmd_status)
    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
