"""review-core history: durable runs, findings, outcomes and escapes, plus retro, compaction and
precision. Design: ../design/outcomes-and-retro.md.

Storage (one file per event, so branches never conflict; closed months compacted on main):

    <root>/<repo>/runs/<run_id>.json
    <root>/<repo>/findings/<run_id>.jsonl
    <root>/<repo>/outcomes/<key-slug>-<hash>.<stamp>.json
    <root>/<repo>/escapes/<fix-sha12>.json
    <root>/<repo>/retro/<yyyy-mm>.json
    <root>/<repo>/archive/<yyyy-mm>.jsonl.gz      closed months, lossless (expired: free text removed)
    <root>/<repo>/aggregates.json

<root> is $REVIEW_HISTORY_DIR, else $CLAUDE_CONFIG_DIR/review-history, else the config
checkout this skill lives in. Every command reads git and GitHub only (`gh api` GETs), so it works
with the agent's read-only token. The records are plain files; versioning them is optional.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import review as rv

SCHEMA = "review-core/2"
KINDS = ("independent", "author-quick", "test")
TRUE_POS = {"fixed", "accepted-risk", "wontfix", "dismissed-real-bug", "accepted-materialised"}
FALSE_POS = {"false-positive"}
EXCLUDED = {"superseded", "unlabelled", "disputed"}            # disputed counts as FP only once confirmed
OUTCOMES = TRUE_POS | FALSE_POS | EXCLUDED
SUPPRESSING = {"false-positive", "accepted-risk", "wontfix"}   # also written to suppressions.md
FREE_TEXT = ("summary", "reason", "intent", "note", "message")
DETAIL_MONTHS = 12
FIX_RX = re.compile(r"\b(fix(e[sd])?|bug|revert|hotfix|regression)\b", re.IGNORECASE)
MERGE_PR_RX = re.compile(r"Merge pull request #(\d+)")
TARGET_PR_RX = re.compile(r"^(?:PR|MR) (\d+)\b")


# =========================================================================== storage
def history_root() -> Path:
    if env := os.environ.get("REVIEW_HISTORY_DIR"):
        return Path(env)
    if cfg := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(cfg) / "review-history"
    return Path(__file__).resolve().parents[3] / "review-history"


def repo_key(repo: Path) -> str:
    """The repo's name from its origin remote (PR worktrees share it), else the folder name."""
    _, project = rv.origin_project(repo)
    return project.rsplit("/", 1)[-1] if project else repo.resolve().name


def _dir(repo: str, *parts: str) -> Path:
    d = history_root().joinpath(repo, *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _month(ts: str) -> str:
    return ts[:7]


def _h(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def finding_key(repo: str, fid: str) -> str:
    """Stable across runs and lenses: repo + the id without its lens prefix."""
    return f"{repo}:{fid.split(':', 1)[-1]}"


def _slug(key: str) -> str:
    """Short, filesystem-safe, unique per key. Kept short so repo-relative paths stay ~110 chars:
    Windows checkouts without core.longpaths fail at 260 chars including the base path."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:40] + "-" + _h(key, 8)


def outcome_name(ev: dict[str, Any]) -> str:
    """<slug>.<yyyymmddhhmmss><seq4>.json (the stamp drops the UTC offset)."""
    return f"{_slug(ev['finding_key'])}.{re.sub(r'[^0-9]', '', ev['at'][:19])}{int(ev.get('seq', 0)) % 10000:04d}.json"


# --------------------------------------------------------------------------- reading (loose + archive)
def _loose(repo: str) -> Iterable[tuple[str, str, Any]]:
    """(type, relative path, record) for every loose file of one repo."""
    base = history_root() / repo
    for typ, sub, pattern in (("run", "runs", "*.json"), ("findings", "findings", "*.jsonl"),
                              ("outcome", "outcomes", "*.json"), ("escape", "escapes", "*.json"),
                              ("retro", "retro", "*.json")):
        for p in sorted((base / sub).glob(pattern)) if (base / sub).is_dir() else []:
            text = p.read_text(encoding="utf-8")
            rec: Any = ([json.loads(ln) for ln in text.splitlines() if ln.strip()] if typ == "findings"
                        else json.loads(text))
            yield typ, f"{sub}/{p.name}", rec


def _archived(repo: str) -> Iterable[tuple[str, str, Any]]:
    d = history_root() / repo / "archive"
    for p in sorted(d.glob("*.jsonl.gz")) if d.is_dir() else []:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    e = json.loads(line)
                    yield e["type"], e["path"], e["rec"]


def load(repo: str) -> dict[str, Any]:
    """All records of one repo: runs by id, findings by run id, outcome events, escapes."""
    data: dict[str, Any] = {"runs": {}, "findings": {}, "outcomes": [], "escapes": [], "retro": []}
    for typ, _path, rec in [*_archived(repo), *_loose(repo)]:
        if typ == "run":
            data["runs"][rec["run_id"]] = rec
        elif typ == "findings":
            if rec:
                data["findings"][rec[0]["run_id"]] = rec
        elif typ == "outcome":
            data["outcomes"].append(rec)
        elif typ == "escape":
            data["escapes"].append(rec)
        else:
            data["retro"].append(rec)
    data["outcomes"].sort(key=lambda e: (e["at"], e.get("seq", 0)))
    return data


def repos() -> list[str]:
    root = history_root()
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def latest_outcomes(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """finding_key -> its latest outcome event (later events win; all are kept on disk)."""
    out: dict[str, dict[str, Any]] = {}
    for e in data["outcomes"]:
        out[e["finding_key"]] = e
    return out


# =========================================================================== run records (from `report`)
def skill_version() -> str:
    skill = Path(__file__).resolve().parents[1]
    parts = [(skill / n).read_text(encoding="utf-8") for n in ("SKILL.md", "contract.md", "rubric.md", "agents.md")
             if (skill / n).exists()]
    return _h("\n".join(parts))


def _lens_versions(lenses: list[dict[str, Any]]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for lens in lenses:
        p = Path(lens.get("path", ""))
        out[lens["name"]] = _h(p.read_text(encoding="utf-8")) if p.is_file() else None
    return out


def make_run_id(repo: str, head_sha: str, effort: str, reviewer_inputs: list[str]) -> str:
    """Same review inputs -> same id, so a re-run of `report` (dry run, then --post) overwrites."""
    return _h("\n".join([repo, head_sha, effort, *sorted(_h(x, 64) for x in reviewer_inputs)]), 16)


def record_run(report: dict[str, Any], scope: dict[str, Any], lenses: list[dict[str, Any]], *,
               reviewer_inputs: list[str], kind: str, model: str, review_wall_s: int | None,
               read_code: Any) -> dict[str, Any]:
    """Write the run + its findings, then infer outcomes for the previous run of the same PR/branch.
    `read_code(path) -> str | None` reads a file as reviewed (working tree, PR worktree or head)."""
    origin = Path(scope.get("origin_repo", scope["repo"]))
    repo = repo_key(origin)
    pr = (scope.get("pr") or {}).get("number")
    branch = None if pr else scope.get("branch")   # only branch/working-tree scopes chain as rounds
    # One run per `prep`: every `report` from the same work dir (a dry run, --post, an added red-team
    # findings file) updates one record. Without a prep stamp, fall back to the reviewer inputs.
    run_id = make_run_id(repo, report["head_sha"], report["effort"],
                         [f"prep:{scope['prepared_at']}"] if scope.get("prepared_at") else reviewer_inputs)
    findings = report["findings"]
    tokens = report["ledger"].get("tokens") or {}
    run = {
        "schema": SCHEMA, "run_id": run_id, "repo": repo, "pr": pr, "branch": branch, "target": scope["target"],
        "base_sha": (scope.get("pr") or {}).get("merge_base") or scope.get("base"), "head_sha": report["head_sha"],
        "kind": kind, "effort": report["effort"], "fresh_context": report["effort"] != "quick",
        "reviewers": report["ledger"]["reviewers"], "verdict": report["verdict"], "ts": _iso(),
        "counts": {s: sum(f["severity"] == s for f in findings) for s in rv.SEV},
        "evidence": {e: sum(f["evidence"] == e for f in findings) for e in rv.EVIDENCE},
        "refuter": report["ledger"]["refuter"], "suppressed": report["ledger"]["suppressed"],
        "per_lens": {n: sum(n in f["lens"].split("+") for f in findings) for n in report["ledger"]["lenses"]},
        "tokens": tokens, "cost_missing": not tokens, "review_wall_s": review_wall_s,
        "skill_version": skill_version(), "model": model, "lens_versions": _lens_versions(lenses),
        "intent": report.get("intent", ""), "prepared_at": scope.get("prepared_at"),
    }
    _write(_dir(repo, "runs") / f"{run_id}.json", run)
    rows = [{"run_id": run_id, "id": f["id"], "finding_key": finding_key(repo, f["id"]), "lens": f["lens"],
             "severity": f["severity"], "evidence": f["evidence"], "blast": f["blast"], "file": f["file"],
             "symbol": f.get("symbol") or "", "line": f.get("line"), "code_fp": f.get("code_fp"),
             "summary": f["summary"], "refuter": f.get("refuter", "unrefuted")} for f in findings]
    path = _dir(repo, "findings") / f"{run_id}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    if kind != "test":
        infer_from_previous(repo, run, rows, read_code)
    return run


def _code_now(f: dict[str, Any], text: str | None) -> str | None:
    """The finding's code fingerprint in `text`; None when the file is gone or, for Python, when its
    cited symbol no longer exists (the line ± 2 fallback would read a rewrite as a fix)."""
    if text is None:
        return None
    if f["file"].endswith(".py") and f.get("symbol"):
        sym = f["symbol"]
        if not any(q == sym or q.endswith("." + sym) for q, _, _ in rv._def_spans_text(text)):
            return None
    return rv.code_fingerprint(Path(f["file"]), f, text)


def _same_defect(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["finding_key"] == b["finding_key"]:
        return True
    return (bool(a["symbol"]) and a["symbol"] == b["symbol"] and a["file"] == b["file"]
            and a["severity"] == b["severity"]
            and abs(int(a.get("line") or 0) - int(b.get("line") or 0)) <= rv.MERGE_LINES)


def previous_run(data: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    """The latest earlier non-test run of the same PR (or, without a PR, the same branch). Runs with
    neither (an explicit commit or range, --staged) never chain: they aren't rounds of anything."""
    if not run["pr"] and not run["branch"]:
        return None
    same = [r for r in data["runs"].values() if r["run_id"] != run["run_id"] and r["kind"] != "test"
            and r["ts"] <= run["ts"]
            and ((run["pr"] and r.get("pr") == run["pr"]) or (not run["pr"] and r.get("branch") == run["branch"]
                                                              and not r.get("pr")))]
    return max(same, key=lambda r: r["ts"], default=None)


def infer_from_previous(repo: str, run: dict[str, Any], rows: list[dict[str, Any]], read_code: Any) -> list[str]:
    """Findings of the previous run that this run no longer reports: code changed -> fixed; code gone
    -> superseded; unchanged -> unlabelled (flagged). Human labels are never overwritten."""
    data = load(repo)
    prev = previous_run(data, run)
    if prev is None:
        return []
    latest = latest_outcomes(data)
    notes = []
    for old in data["findings"].get(prev["run_id"], []):
        if any(_same_defect(old, new) for new in rows):
            continue
        if (cur := latest.get(old["finding_key"])) and cur["source"] in ("human", "attribution"):
            continue
        text = read_code(old["file"])
        now = _code_now(old, text)
        if now is None:
            outcome, why = "superseded", "cited file or symbol no longer exists"
        elif old.get("code_fp") and now != old["code_fp"]:
            outcome, why = "fixed", f"cited code changed and run {run['run_id']} no longer reports it"
        else:
            outcome, why = "unlabelled", "no longer reported although the cited code is unchanged: needs a label"
        add_outcome(repo, old, outcome, source="inferred", reason=why, run_id=run["run_id"])
        notes.append(f"{old['id']}: {outcome}")
    return notes


# =========================================================================== outcome events
_seq = 0


def add_outcome(repo: str, finding: dict[str, Any], outcome: str, *, source: str, reason: str,
                run_id: str | None = None, by: str | None = None) -> dict[str, Any]:
    global _seq
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    _seq += 1
    ev = {"finding_key": finding["finding_key"], "id": finding["id"], "lens": finding["lens"],
          "severity": finding["severity"], "outcome": outcome, "source": source, "reason": reason,
          "run_id": run_id or finding.get("run_id"), "by": by or source, "at": _iso(), "seq": _seq}
    _write(_dir(repo, "outcomes") / outcome_name(ev), ev)
    return ev


def normalize_names(repo: str) -> int:
    """Rename loose outcome files to the current short form (older ones had ~117-char names).
    Idempotent; a name collision keeps both files, the newcomer suffixed with a content hash."""
    d = history_root() / repo / "outcomes"
    n = 0
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        ev = json.loads(p.read_text(encoding="utf-8"))
        want = d / outcome_name(ev)
        if p.name == want.name:
            continue
        if want.exists():
            want = d / f"{want.stem}-{_h(p.read_text(encoding='utf-8'), 6)}.json"
        p.rename(want)
        n += 1
    return n


def _find(data: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """A finding by its id, finding_key or unique key suffix; the latest run's copy."""
    hits = [f for rows in data["findings"].values() for f in rows
            if ref in (f["id"], f["finding_key"]) or f["finding_key"].endswith(ref)]
    by_key = {f["finding_key"]: f for f in sorted(hits, key=lambda f: data["runs"].get(f["run_id"], {}).get("ts", ""))}
    if len(by_key) > 1:
        raise SystemExit(f"{ref!r} matches {len(by_key)} findings; use the full id")
    return next(iter(by_key.values()), None)


def label(repo_path: Path, ref: str, outcome: str, reason: str, *, by: str) -> dict[str, Any]:
    """Human label (or the author's `disputed`). Suppressing outcomes also go to suppressions.md."""
    if by == "author" and outcome != "disputed":
        raise SystemExit("the author agent may only mark findings 'disputed'; only a human sets other outcomes")
    if outcome not in OUTCOMES - {"unlabelled"}:
        raise SystemExit(f"outcome must be one of {sorted(OUTCOMES - {'unlabelled'})}")
    if not reason.strip():
        raise SystemExit("a reason is required")
    repo = repo_key(repo_path)
    f = _find(load(repo), ref)
    if f is None:
        raise SystemExit(f"no recorded finding matches {ref!r} in {repo}")
    ev = add_outcome(repo, f, outcome, source="author" if by == "author" else "human", reason=reason, by=by)
    if outcome in SUPPRESSING:
        sup = repo_path / rv.PACK_REL / "suppressions.md"
        sup.parent.mkdir(parents=True, exist_ok=True)
        existing = sup.read_text(encoding="utf-8") if sup.exists() else "# Suppressions\n"
        if f"- {f['id']} " not in existing:
            sup.write_text(existing.rstrip("\n") + f"\n- {f['id']} — {outcome} — {reason} — {_iso()[:10]}\n",
                           encoding="utf-8")
    return ev


# =========================================================================== outcomes at merge
def _gh() -> str:
    return rv.find_gh()


def gh_json(args: list[str]) -> Any:
    proc = subprocess.run([_gh(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise rv.PostError(f"gh {' '.join(args[:3])}: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout or "null")


def _project(repo_path: Path) -> str:
    _, project = rv.origin_project(repo_path)
    if not project:
        raise SystemExit(f"{repo_path} has no GitHub origin")
    return project


def _show(repo_path: Path, rev: str, path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo_path), "show", f"{rev}:{path}"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.stdout if proc.returncode == 0 else None


def pr_runs(data: dict[str, Any], pr: int, branch: str | None = None,
            until: str | None = None) -> list[dict[str, Any]]:
    """Non-test runs of a PR: those recorded with its number, plus branch reviews of its head branch
    (the author's local rounds) made before it merged (`until`)."""
    def belongs(r: dict[str, Any]) -> bool:
        if r.get("pr") == pr:
            return True
        return bool(branch) and not r.get("pr") and r.get("branch") == branch and (not until or r["ts"] <= until)
    return sorted((r for r in data["runs"].values() if r["kind"] != "test" and belongs(r)), key=lambda r: r["ts"])


_PR_INFO: dict[tuple[str, int], dict[str, Any]] = {}


def pr_info(repo_path: Path, pr: int) -> dict[str, Any]:
    """GitHub's view of a PR (head branch, merge commit, merged_at); {} when offline."""
    key = (str(repo_path), pr)
    if key not in _PR_INFO:
        try:
            _PR_INFO[key] = gh_json(["api", f"repos/{_project(repo_path)}/pulls/{pr}"]) or {}
        except (rv.PostError, SystemExit, json.JSONDecodeError):
            _PR_INFO[key] = {}
    return _PR_INFO[key]


def runs_for_pr(repo_path: Path, data: dict[str, Any], pr: int) -> list[dict[str, Any]]:
    info = pr_info(repo_path, pr)
    until = (info.get("merged_at") or "").replace("Z", "+00:00") or None
    return pr_runs(data, pr, (info.get("head") or {}).get("ref"), until)


def merge_outcomes(repo_path: Path, pr: int, info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """For a merged PR: each finding of its last review still without an outcome, compared with the
    merged code. Changed -> suggest `fixed`; unchanged -> shipped as-is, needs a label. Writes nothing."""
    repo = repo_key(repo_path)
    data = load(repo)
    info = info or gh_json(["api", f"repos/{_project(repo_path)}/pulls/{pr}"])
    until = (info.get("merged_at") or "").replace("Z", "+00:00") or None
    runs = pr_runs(data, pr, (info.get("head") or {}).get("ref"), until)
    if not runs:
        return []
    merge_sha = info.get("merge_commit_sha") if info.get("merged_at") or info.get("merged") else None
    if not merge_sha:
        raise SystemExit(f"PR {pr} is not merged")
    rv.git(repo_path, "fetch", "--quiet", "origin", merge_sha, check=False)
    if subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", f"{merge_sha}^{{commit}}"],
                      capture_output=True).returncode != 0:      # else every file would read as "removed"
        raise SystemExit(f"merge commit {merge_sha[:9]} of PR {pr} is not available locally: "
                         "run `git fetch origin` and retry")
    latest = latest_outcomes(data)
    rows = []
    for f in data["findings"].get(runs[-1]["run_id"], []):
        cur = latest.get(f["finding_key"])
        if cur and cur["outcome"] != "unlabelled":
            continue
        text = _show(repo_path, merge_sha, f["file"])
        now = _code_now(f, text)
        if now is None:
            state, suggest = "removed at merge", "superseded"
        elif f.get("code_fp") and now != f["code_fp"]:
            state, suggest = "changed before merge", "fixed"
        else:
            state, suggest = "shipped as-is", None
        rows.append({"finding": f, "state": state, "suggest": suggest})
    return rows


def accept_suggestions(repo_path: Path, rows: list[dict[str, Any]], reason: str) -> int:
    repo = repo_key(repo_path)
    n = 0
    for r in rows:
        if r["suggest"]:
            add_outcome(repo, r["finding"], r["suggest"], source="human", by="human",
                        reason=f"{reason} ({r['state']})")
            n += 1
    return n


# =========================================================================== bug attribution (SZZ)
OLD_HUNK_RX = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _blame_lines(repo_path: Path, rev: str, path: str, lines: list[int],
                 origins: dict[str, set[str]] | None = None) -> Counter[str]:
    """commit -> number of the given lines (at `rev`) it last introduced, ignoring whitespace and
    following moved/copied code; skips commits listed in .git-blame-ignore-revs. `origins` collects
    each commit's file name at the time (a later rename must not hide the reviewed path)."""
    if not lines:
        return Counter()
    args = ["git", "-C", str(repo_path), "blame", "-w", "-M", "-C", "--line-porcelain"]
    if (repo_path / ".git-blame-ignore-revs").exists():
        args += ["--ignore-revs-file", ".git-blame-ignore-revs"]
    for ln in sorted(set(lines)):
        args += ["-L", f"{ln},{ln}"]
    proc = subprocess.run([*args, rev, "--", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    shas: list[str] = []
    sha: str | None = None
    for out in proc.stdout.splitlines():
        if re.match(r"^[0-9a-f]{40} \d+ \d+", out):
            sha = out.split()[0]
            shas.append(sha)
        elif sha and out.startswith("filename ") and origins is not None:
            origins.setdefault(sha, set()).add(out[len("filename "):])
    return Counter(shas)


def _enclosing(text: str, line: int) -> tuple[str, int, int] | None:
    spans = [s for s in rv._def_spans_text(text) if s[1] <= line <= s[2]]
    return min(spans, key=lambda s: s[2] - s[1], default=None)


def fix_changes(repo_path: Path, fix: str) -> list[dict[str, Any]]:
    """Per file: old-side lines the fix changed or deleted, the symbols they sit in, and whether the
    fix only added code there (a missing check: nothing to blame directly)."""
    out = []
    diff = rv.git(repo_path, *rv.QUOTE_OFF, "diff", *rv.DIFF_U0, f"{fix}^", fix, check=False)
    cur: dict[str, Any] | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            old = rv.git_path(line[4:].rstrip("\t"))
            cur = None if old == "/dev/null" else {"path": old.removeprefix("a/"), "lines": [], "added_at": []}
            if cur:
                out.append(cur)
        elif cur and (m := OLD_HUNK_RX.match(line)):
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                cur["lines"] += range(start, start + count)
            else:
                cur["added_at"].append(start)
    return out


def attribute(repo_path: Path, fix: str, intro_pr: int | None = None) -> dict[str, Any] | None:
    """Candidate attribution of a fix commit to the PR that introduced the bug, classified against
    that PR's review history. Writes nothing; `confirm_escape` records it."""
    fix = rv.git(repo_path, "rev-parse", fix).strip()
    parent = f"{fix}^"
    blamed: Counter[str] = Counter()
    origins: dict[str, set[str]] = {}
    symbols: set[str] = set()
    low = False
    changes = fix_changes(repo_path, fix)
    for ch in changes:
        text = _show(repo_path, parent, ch["path"]) or ""
        lines = list(ch["lines"])
        for at in ch["added_at"]:                        # add-only: blame the enclosing function
            if enc := _enclosing(text, max(at, 1)):
                lines += range(enc[1], enc[2] + 1)
                low = True
        for ln in ch["lines"] + ch["added_at"]:
            if enc := _enclosing(text, max(ln, 1)):
                symbols.add(f"{ch['path']}::{enc[0]}")
        blamed += _blame_lines(repo_path, parent, ch["path"], lines, origins)
    if not blamed:
        return None
    intro, n = blamed.most_common(1)[0]
    share = n / sum(blamed.values())
    fix_pr = _pr_of(repo_path, fix)
    intro_pr = intro_pr if intro_pr is not None else _pr_of(repo_path, intro)   # a human override wins
    if intro_pr is not None and intro_pr == fix_pr:
        return None                                      # fixed inside the same PR: not an escape
    repo = repo_key(repo_path)
    data = load(repo)
    runs = runs_for_pr(repo_path, data, intro_pr) if intro_pr else []
    files = {c["path"] for c in changes} | origins.get(intro, set())   # incl. the name it had then
    names = {s.split("::", 1)[1] for s in symbols}

    def covers(f: dict[str, Any]) -> bool:               # same file (either name) and same function
        if f["file"] not in files:
            return False
        if not names or not f["symbol"]:
            return not names
        return any(n == f["symbol"] or n.endswith("." + f["symbol"]) or f["symbol"].endswith("." + n) for n in names)
    covering = [f for r in runs for f in data["findings"].get(r["run_id"], []) if covers(f)]
    latest = latest_outcomes(data)
    if not runs:
        klass = "unreviewed"
    elif not covering:
        klass = "missed"
    else:
        # Coverage is matched by file + function, so a covering finding may be about another defect
        # there: the class is provisional until a human confirms it (`escape --confirm --class …`).
        outs = {(latest.get(f["finding_key"]) or {}).get("outcome") for f in covering}
        klass = ("accepted-materialised" if outs & {"accepted-risk", "wontfix"}
                 else "dismissed-real-bug" if outs & {"false-positive", "disputed"}
                 else "flagged-unlabelled" if outs & {"unlabelled", None}
                 else "missed")                          # it was fixed in review, yet the bug came back
    intro_ts = rv.git(repo_path, "show", "-s", "--format=%cI", intro).strip()
    fix_ts = rv.git(repo_path, "show", "-s", "--format=%cI", fix).strip()
    lag = (dt.datetime.fromisoformat(fix_ts) - dt.datetime.fromisoformat(intro_ts)).days if intro_ts and fix_ts else None
    return {"fix": fix, "fix_pr": fix_pr, "fix_message": rv.git(repo_path, "show", "-s", "--format=%s", fix).strip(),
            "introduced_by": intro, "introduced_pr": intro_pr, "files": sorted(files), "symbols": sorted(symbols),
            "class": klass, "provisional": bool(covering), "covering": [f["id"] for f in covering], "lag_days": lag,
            "confidence": "low" if low or share < 0.5 else "medium" if share < 0.8 else "high",
            "blamed": dict(blamed)}


def _remote_default(repo_path: Path) -> str:
    """origin/<default> when fetched (what was merged on GitHub), else the local default branch."""
    name = rv.default_branch(repo_path)
    remote = f"origin/{name}"
    return remote if rv.git(repo_path, "rev-parse", "--verify", "--quiet", remote, check=False).strip() else name


def _pr_of(repo_path: Path, sha: str) -> int | None:
    """The PR that brought `sha` into the default branch: GitHub's commit->PR link, else the first
    'Merge pull request #N' merge commit on its ancestry path."""
    try:
        prs = gh_json(["api", f"repos/{_project(repo_path)}/commits/{sha}/pulls"])
        merged = [p["number"] for p in prs or [] if p.get("merged_at")]
        if merged:
            return min(merged)
    except (rv.PostError, SystemExit, json.JSONDecodeError):
        pass
    default = _remote_default(repo_path)
    log = rv.git(repo_path, "log", "--merges", "--ancestry-path", "--reverse", "--format=%s",
                 f"{sha}..{default}", check=False)
    m = next((MERGE_PR_RX.search(s) for s in log.splitlines() if MERGE_PR_RX.search(s)), None)
    return int(m.group(1)) if m else None


ESCAPE_CLASSES = ("missed", "dismissed-real-bug", "accepted-materialised", "unreviewed", "not-a-bug")


def confirm_escape(repo_path: Path, cand: dict[str, Any], *, by: str = "human", note: str = "",
                   klass: str | None = None) -> dict[str, Any]:
    """Record a human-confirmed attribution (`klass` overrides the suggested class; `not-a-bug`
    records a rejected candidate so retro stops listing it), and correct covering findings' outcomes."""
    repo = repo_key(repo_path)
    if klass is None and cand.get("provisional"):
        raise SystemExit(f"the suggested class {cand['class']!r} is provisional (a finding in the same function, maybe "
                         f"another defect: {', '.join(cand['covering']) or 'none'}); pass --class with the human's decision")
    final = klass or cand["class"]
    if final not in ESCAPE_CLASSES:
        raise SystemExit(f"choose the class: one of {', '.join(ESCAPE_CLASSES)} "
                         f"(suggested: {cand['class']}; covering: {', '.join(cand['covering']) or 'none'})")
    cand = {**cand, "class": final, "provisional": False}
    rec = {**cand, "confirmed_by": by, "confirmed_at": _iso(), "note": note}
    _write(_dir(repo, "escapes") / f"{cand['fix'][:12]}.json", rec)
    if cand["class"] in ("dismissed-real-bug", "accepted-materialised"):
        data = load(repo)
        for fid in cand["covering"]:
            if f := _find(data, fid):
                add_outcome(repo, f, cand["class"], source="attribution", by=by,
                            reason=f"bug fixed in {cand['fix'][:9]} traces back to this finding")
    return rec


def manual_escape(repo_path: Path, path: str, symbol: str, note: str, pr: int | None) -> dict[str, Any]:
    """A bug with no fix yet (or found in another repo): attribute by file + symbol to `pr`."""
    cand: dict[str, Any] = {"fix": "manual-" + _h(f"{path}:{symbol}:{note}", 12), "fix_pr": None, "fix_message": note,
            "introduced_by": None, "introduced_pr": pr, "files": [path], "symbols": [f"{path}::{symbol}"],
            "covering": [], "lag_days": None, "confidence": "manual", "blamed": {}}
    data = load(repo_key(repo_path))
    runs = runs_for_pr(repo_path, data, pr) if pr else []
    cover = [f["id"] for r in runs for f in data["findings"].get(r["run_id"], [])
             if f["file"] == path and (not symbol or f["symbol"] == symbol)]
    cand.update(covering=cover, provisional=bool(cover),
                **{"class": "unreviewed" if not runs else "missed" if not cover else "flagged-unlabelled"})
    return cand


# =========================================================================== retro
def _period(month: str | None, since: str | None) -> tuple[dt.datetime, dt.datetime, str]:
    if month:
        start = dt.datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=dt.UTC)
        end = (start + dt.timedelta(days=32)).replace(day=1)
        return start, end, month
    start = dt.datetime.strptime(since or (_now() - dt.timedelta(days=30)).strftime("%Y-%m-%d"),
                                 "%Y-%m-%d").replace(tzinfo=dt.UTC)
    return start, _now(), f"since-{start:%Y%m%d}"


def _human_comments(project: str, pr: int) -> list[str]:
    out = []
    for path in (f"repos/{project}/issues/{pr}/comments", f"repos/{project}/pulls/{pr}/comments"):
        try:
            pages = gh_json(["api", "--paginate", "--slurp", path])
        except rv.PostError:
            continue
        for c in (x for page in pages or [] for x in (page if isinstance(page, list) else [page])):
            body = c.get("body") or ""
            if "<!-- review-core:" not in body:
                out.append(rv._clip(body.replace("\n", " "), 1))
    return out


def recover_from_comments(project: str, pr: int) -> list[str]:
    """Finding ids recoverable from a PR's posted review-core comments (local history missing)."""
    ids: set[str] = set()
    for path in (f"repos/{project}/issues/{pr}/comments", f"repos/{project}/pulls/{pr}/comments"):
        try:
            pages = gh_json(["api", "--paginate", "--slurp", path])
        except rv.PostError:
            continue
        for c in (x for page in pages or [] for x in (page if isinstance(page, list) else [page])):
            ids |= set(rv.ID_MARK_RX.findall(c.get("body") or ""))
    return sorted(ids)


def fix_commits(repo_path: Path, start: dt.datetime, end: dt.datetime, bug_prs: set[int]) -> list[str]:
    """Non-merge commits on the default branch in the period whose message reads like a fix, plus
    every commit of PRs labelled `bug` that merged in the period."""
    default = _remote_default(repo_path)
    out = []
    log = rv.git(repo_path, "log", default, "--no-merges", f"--since={start:%Y-%m-%dT%H:%M:%S}",
                 f"--until={end:%Y-%m-%dT%H:%M:%S}", "--format=%H%x09%s", check=False)
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if FIX_RX.search(subject):
            out.append(sha)
    for n in sorted(bug_prs):
        try:
            commits = gh_json(["api", f"repos/{_project(repo_path)}/pulls/{n}/commits"])
            out += [c["sha"] for c in commits or [] if c["sha"] not in out]
        except rv.PostError:
            pass
    return out


def retro(repo_path: Path, month: str | None = None, since: str | None = None,
          merged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Merged PRs and fix commits in the period: process stats, findings still unlabelled, and
    candidate bug attributions for a human to confirm. Writes only retro/<period>.json."""
    repo = repo_key(repo_path)
    project = _project(repo_path)
    start, end, label_ = _period(month, since)
    rv.git(repo_path, "fetch", "--quiet", "origin", check=False)
    if merged is None:
        merged = gh_json(["pr", "list", "--repo", project, "--state", "merged", "--limit", "300",
                          "--json", "number,title,mergedAt,createdAt,labels,headRefName"])
    in_period = [p for p in merged or []
                 if start <= dt.datetime.fromisoformat(p["mergedAt"].replace("Z", "+00:00")) < end]
    data = load(repo)
    latest = latest_outcomes(data)
    prs = []
    # A branch can carry several PRs over time: each branch review belongs to the first PR from that
    # branch that merged after it, never to a later one too.
    claimed: set[str] = set()
    assigned: dict[int, list[dict[str, Any]]] = {}
    for p in sorted(merged or [], key=lambda p: p["mergedAt"]):
        runs = [r for r in pr_runs(data, p["number"], p.get("headRefName"), p["mergedAt"].replace("Z", "+00:00"))
                if r.get("pr") == p["number"] or r["run_id"] not in claimed]
        claimed |= {r["run_id"] for r in runs if not r.get("pr")}
        assigned[p["number"]] = runs
    for p in sorted(in_period, key=lambda p: p["number"]):
        runs = assigned.get(p["number"], [])
        last = data["findings"].get(runs[-1]["run_id"], []) if runs else []
        unlabelled = [f["id"] for f in last if (latest.get(f["finding_key"]) or {}).get("outcome") in (None, "unlabelled")]
        merged_at = dt.datetime.fromisoformat(p["mergedAt"].replace("Z", "+00:00"))
        first = dt.datetime.fromisoformat(runs[0]["ts"]) if runs else None
        prs.append({
            "pr": p["number"], "title": p["title"], "merged_at": p["mergedAt"],
            "reviews": len(runs), "efforts": [r["effort"] for r in runs],
            "first_review_to_merge_h": round((merged_at - first).total_seconds() / 3600, 1) if first else None,
            "findings_per_round": [sum(r["counts"].values()) for r in runs],
            "p0_p1_at_merge": [f["id"] for f in last if f["severity"] in ("P0", "P1")
                               and (latest.get(f["finding_key"]) or {}).get("outcome") not in ("fixed", "superseded")],
            "verdicts": [r["verdict"] for r in runs], "lenses": sorted({ln for r in runs for ln in r["per_lens"]}),
            "tokens": sum((r.get("tokens") or {}).get("total", 0) for r in runs),
            "cost_missing": any(r.get("cost_missing") for r in runs),
            "unlabelled": unlabelled,
            "recovered_from_comments": recover_from_comments(project, p["number"]) if not runs else [],
            "human_comments": _human_comments(project, p["number"]),
        })
    bug_prs = {p["number"] for p in in_period if any(lb.get("name") == "bug" for lb in p.get("labels") or [])}
    known = {e["fix"] for e in data["escapes"]}
    candidates = []
    for sha in fix_commits(repo_path, start, end, bug_prs):
        if sha in known:
            continue
        if cand := attribute(repo_path, sha):
            candidates.append(cand)
    older = [f["id"] for r in data["runs"].values() if r["kind"] == "independent"
             for f in data["findings"].get(r["run_id"], [])
             if (latest.get(f["finding_key"]) or {}).get("outcome") in (None, "unlabelled")]
    out = {"schema": SCHEMA, "repo": repo, "period": label_, "start": _iso(start), "end": _iso(end), "at": _iso(),
           "prs": prs, "escape_candidates": candidates, "unlabelled_anywhere": sorted(set(older))}
    _write(_dir(repo, "retro") / f"{label_}.json", out)
    return out


def render_retro(r: dict[str, Any]) -> str:
    def cell(x: Any) -> str:
        return str(x).replace("|", "\\|")
    lines = [f"## Retro · {r['repo']} · {r['period']}", ""]
    if r["prs"]:
        lines += ["| PR | Reviews | Efforts | Findings/round | Verdicts | P0/P1 at merge | Review→merge | Tokens | Unlabelled |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for p in r["prs"]:
            lines.append(f"| {p['pr']} {cell(p['title'][:40])} | {p['reviews']} | {','.join(p['efforts']) or '—'} | "
                         f"{p['findings_per_round'] or '—'} | {','.join(p['verdicts']) or '—'} | "
                         f"{len(p['p0_p1_at_merge'])} | {p['first_review_to_merge_h'] if p['first_review_to_merge_h'] is not None else '—'} h | "
                         f"{p['tokens'] or ('missing' if p['cost_missing'] else '—')} | {len(p['unlabelled'])} |")
        for p in r["prs"]:
            if p["recovered_from_comments"]:
                lines.append(f"- PR {p['pr']}: no local history; {len(p['recovered_from_comments'])} finding id(s) "
                             "recovered from its posted comments")
            if p["human_comments"]:
                lines.append(f"- PR {p['pr']}: {len(p['human_comments'])} non-review-core comment(s): check for "
                             "anything the review missed")
    else:
        lines.append("No PRs merged in this period.")
    lines += ["", "### Bug attribution candidates (confirm each)"]
    if r["escape_candidates"]:
        lines += ["| Fix | Introduced by | Class | Lag | Confidence | Files |", "|---|---|---|---|---|---|"]
        for c in r["escape_candidates"]:
            lines.append(f"| {c['fix'][:9]} {cell(c['fix_message'][:40])} | "
                         f"{('PR ' + str(c['introduced_pr'])) if c['introduced_pr'] else c['introduced_by'][:9]} | "
                         f"{c['class']}{' (provisional)' if c.get('provisional') else ''} | {c['lag_days']} d | {c['confidence']} | {', '.join(c['files'])[:50]} |")
        lines.append("Confirm each with `review.py escape --repo <repo> --fix <sha> --confirm --class <class>` "
                     "(classes: missed, dismissed-real-bug, accepted-materialised, unreviewed, not-a-bug). "
                     "A provisional class (a finding in the same function, maybe another defect) needs a human call.")
    else:
        lines.append("None.")
    if r["unlabelled_anywhere"]:
        lines += ["", f"### {len(r['unlabelled_anywhere'])} finding(s) still need a label",
                  *(f"- `{fid}`" for fid in r["unlabelled_anywhere"][:20])]
    return "\n".join(lines)


# =========================================================================== compaction
def _months_ago(month: str, now: dt.datetime) -> int:
    y, m = map(int, month.split("-"))
    return (now.year - y) * 12 + (now.month - m)


def _record_month(typ: str, rec: Any) -> str:
    if typ == "findings":
        return ""                                        # follows its run
    return _month(rec.get("ts") or rec.get("at") or rec.get("confirmed_at") or "")


def dedupe_runs(repo: str) -> list[str]:
    """Remove loose runs recorded twice for one review (before run ids were per `prep`, a re-run of
    `report` with an extra findings file made a second run): same head, effort, kind, verdict, tokens
    and finding ids. The earliest is kept, by run id within one second. Outcome events are keyed by finding, so none are lost."""
    base = history_root() / repo
    data = load(repo)
    seen: dict[str, str] = {}
    dropped = []
    for r in sorted(data["runs"].values(), key=lambda r: (r["ts"], r["run_id"])):
        ids = sorted(f["id"] for f in data["findings"].get(r["run_id"], []))
        sig = json.dumps([r["head_sha"], r["effort"], r["kind"], r.get("verdict"), r.get("tokens"), ids],
                         sort_keys=True)
        if sig not in seen:
            seen[sig] = r["run_id"]
            continue
        loose = [base / "runs" / f"{r['run_id']}.json", base / "findings" / f"{r['run_id']}.jsonl"]
        if all(p.exists() for p in loose):              # archived duplicates are left as they are
            for p in loose:
                p.unlink()
            dropped.append(r["run_id"])
    return dropped


def compact(repo: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Archive loose files of closed months (older than last month) into archive/<yyyy-mm>.jsonl.gz,
    verified lossless before the loose files are deleted; expire archives older than DETAIL_MONTHS
    (free text removed, keys and outcomes kept); refresh aggregates.json. Idempotent."""
    now = now or _now()
    base = history_root() / repo
    renamed = normalize_names(repo)
    deduped = dedupe_runs(repo)
    loose = list(_loose(repo))
    run_month = {rec["run_id"]: _month(rec["ts"]) for typ, _, rec in loose if typ == "run"}
    run_month.update({rec["run_id"]: _month(rec["ts"]) for typ, _, rec in _archived(repo) if typ == "run"})
    by_month: dict[str, list[tuple[str, str, Any]]] = defaultdict(list)
    for typ, path, rec in loose:
        month = run_month.get(rec[0]["run_id"], "") if typ == "findings" and rec else _record_month(typ, rec)
        if month and _months_ago(month, now) >= 2:
            by_month[month].append((typ, path, rec))
    archived = []
    for month, entries in sorted(by_month.items()):
        arc = base / "archive" / f"{month}.jsonl.gz"
        old = []
        if arc.exists():
            with gzip.open(arc, "rt", encoding="utf-8") as fh:
                old = [json.loads(ln) for ln in fh if ln.strip()]
        keep = {e["path"]: e for e in old}
        for typ, path, rec in entries:
            keep[path] = {"type": typ, "path": path, "rec": rec}
        body = "".join(json.dumps(keep[p], ensure_ascii=False, sort_keys=True) + "\n" for p in sorted(keep))
        arc.parent.mkdir(parents=True, exist_ok=True)
        tmp = arc.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(body)
        with gzip.open(tmp, "rt", encoding="utf-8") as fh:  # verify before deleting anything
            back = {e["path"]: e["rec"] for e in (json.loads(ln) for ln in fh if ln.strip())}
        if any(back.get(path) != rec for _, path, rec in entries):
            tmp.unlink()
            raise RuntimeError(f"archive {arc.name} did not round-trip; nothing deleted")
        tmp.replace(arc)
        for _, path, _ in entries:
            (base / path).unlink()
        archived.append(month)
    expired = []
    for arc in sorted((base / "archive").glob("*.jsonl.gz")) if (base / "archive").is_dir() else []:
        month = arc.name[:7]
        if _months_ago(month, now) <= DETAIL_MONTHS:
            continue
        with gzip.open(arc, "rt", encoding="utf-8") as fh:
            stored: list[dict[str, Any]] = [json.loads(ln) for ln in fh if ln.strip()]
        if all(e.get("expired") for e in stored):
            continue
        for e in stored:
            recs = e["rec"] if isinstance(e["rec"], list) else [e["rec"]]
            for r in recs:
                for k in FREE_TEXT:
                    r.pop(k, None)
            e["expired"] = True
        with gzip.open(arc, "wt", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in stored))
        expired.append(month)
    agg = aggregates(repo)
    _write(base / "aggregates.json", agg)
    return {"archived": archived, "expired": expired, "renamed": renamed, "deduped": deduped, "aggregate_months": len(agg["months"])}


def prune_reports(repo_path: Path, days: int = 90, now: dt.datetime | None = None) -> int:
    """Delete local full reports in <repo>/.claude/review/out/ older than `days` (not history)."""
    out = repo_path / rv.PACK_REL / "out"
    cutoff = (now or _now()) - dt.timedelta(days=days)
    n = 0
    for p in out.glob("2*.json") if out.is_dir() else []:
        try:
            stamp = dt.datetime.strptime(p.name[:13], "%Y%m%dT%H%M").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if stamp < cutoff:
            p.unlink()
            n += 1
    return n


# =========================================================================== metrics
def wilson_lower(tp: int, n: int, z: float = 1.96) -> float | None:
    if n == 0:
        return None
    p = tp / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _counted_findings(data: dict[str, Any], *, months: int | None, now: dt.datetime,
                      since_lens_change: bool) -> dict[str, dict[str, Any]]:
    """finding_key -> finding (latest copy) from independent runs in the window."""
    runs = {rid: r for rid, r in data["runs"].items() if r["kind"] == "independent"
            and (months is None or _months_ago(_month(r["ts"]), now) < months)}
    current_lens: dict[str, str | None] = {}
    if since_lens_change and runs:
        newest = max(runs.values(), key=lambda r: r["ts"])
        current_lens = newest.get("lens_versions") or {}
    out: dict[str, dict[str, Any]] = {}
    for rid, r in sorted(runs.items(), key=lambda kv: kv[1]["ts"]):
        for f in data["findings"].get(rid, []):
            if since_lens_change:
                lenses = f["lens"].split("+")
                if any((r.get("lens_versions") or {}).get(ln) != current_lens.get(ln) for ln in lenses):
                    continue
            out[f["finding_key"]] = f
    return out


def precision(repo: str | None = None, lens: str | None = None, severities: set[str] | None = None,
              months: int | None = DETAIL_MONTHS, since_lens_change: bool = False,
              now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or _now()
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tp": 0, "fp": 0, "excluded": 0, "unlabelled": 0,
                                                           "prs": set(), "repos": set(), "escapes": Counter()})
    for name in [repo] if repo else repos():
        data = load(name)
        latest = latest_outcomes(data)
        found = _counted_findings(data, months=months, now=now, since_lens_change=since_lens_change)
        for key, f in found.items():
            if severities and f["severity"] not in severities:
                continue
            out = (latest.get(key) or {}).get("outcome")
            for ln in f["lens"].split("+"):
                if lens and ln != lens:
                    continue
                row = rows[ln]
                if out in TRUE_POS:
                    row["tp"] += 1
                elif out in FALSE_POS:
                    row["fp"] += 1
                elif out in (None, "unlabelled"):
                    row["unlabelled"] += 1
                else:
                    row["excluded"] += 1
                row["prs"].add((name, data["runs"].get(f["run_id"], {}).get("pr")))
                row["repos"].add(name)
        for e in (x for x in data["escapes"] if x["class"] != "not-a-bug"):
            if months is None or _months_ago(_month(e["confirmed_at"]), now) < months:
                for rid in {r["run_id"] for r in pr_runs(data, e["introduced_pr"])} if e.get("introduced_pr") else []:
                    for ln in data["runs"][rid]["per_lens"]:
                        if not lens or ln == lens:
                            rows[ln]["escapes"][e["class"]] += 1
    result = {}
    for ln, r in sorted(rows.items()):
        n = r["tp"] + r["fp"]
        result[ln] = {"tp": r["tp"], "fp": r["fp"], "labelled": n, "excluded": r["excluded"],
                      "unlabelled": r["unlabelled"], "precision": round(r["tp"] / n, 3) if n else None,
                      "wilson_lower": round(lb, 3) if (lb := wilson_lower(r["tp"], n)) is not None else None,
                      "prs": len({p for p in r["prs"] if p[1]}), "repos": len(r["repos"]),
                      "escapes": dict(r["escapes"]), "sufficient": n >= 40}
    return result


def render_precision(rows: dict[str, dict[str, Any]], scope: str) -> str:
    lines = [f"## Precision · {scope}", "",
             "| Lens | TP | FP | Labelled | Unlabelled | Excluded | Precision | Wilson 95% low | PRs | Escapes | Sample |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for ln, r in rows.items():
        esc = ", ".join(f"{k} {v}" for k, v in r["escapes"].items()) or "—"
        lines.append(f"| {ln} | {r['tp']} | {r['fp']} | {r['labelled']} | {r['unlabelled']} | {r['excluded']} | "
                     f"{r['precision'] if r['precision'] is not None else '—'} | "
                     f"{r['wilson_lower'] if r['wilson_lower'] is not None else '—'} | {r['prs']} | {esc} | "
                     f"{'ok' if r['sufficient'] else 'insufficient (<40 labelled)'} |")
    if not rows:
        lines.append("| — | no independent reviews in the window | | | | | | | | | |")
    return "\n".join(lines)


def aggregates(repo: str) -> dict[str, Any]:
    """Per lens per month: TP/FP/labelled/excluded/unlabelled, escapes by class, tokens, reviews,
    and the skill/model versions seen. Computed from all tiers; kept forever (small)."""
    data = load(repo)
    latest = latest_outcomes(data)
    months: dict[str, dict[str, Any]] = defaultdict(lambda: {"lenses": defaultdict(Counter), "reviews": 0,
                                                             "tokens": 0, "skill_versions": set(), "models": set(),
                                                             "escapes": Counter()})
    for rid, r in data["runs"].items():
        if r["kind"] != "independent":
            continue
        m = months[_month(r["ts"])]
        m["reviews"] += 1
        m["tokens"] += (r.get("tokens") or {}).get("total", 0)
        m["skill_versions"].add(r.get("skill_version"))
        m["models"].add(r.get("model"))
        for f in data["findings"].get(rid, []):
            out = (latest.get(f["finding_key"]) or {}).get("outcome")
            bucket = ("tp" if out in TRUE_POS else "fp" if out in FALSE_POS
                      else "unlabelled" if out in (None, "unlabelled") else "excluded")
            for ln in f["lens"].split("+"):
                m["lenses"][ln][bucket] += 1
    for e in (x for x in data["escapes"] if x["class"] != "not-a-bug"):
        months[_month(e["confirmed_at"])]["escapes"][e["class"]] += 1
    return {"repo": repo, "updated": _iso(), "months": {
        k: {"lenses": {ln: dict(c) for ln, c in v["lenses"].items()}, "reviews": v["reviews"], "tokens": v["tokens"],
            "skill_versions": sorted(x for x in v["skill_versions"] if x), "models": sorted(x for x in v["models"] if x),
            "escapes": dict(v["escapes"])} for k, v in sorted(months.items())}}


# =========================================================================== backfill
def import_reports(repo_path: Path, report_paths: list[Path], test_names: set[str]) -> dict[str, int]:
    """Import existing report JSONs (review-core/1) as runs. Duplicates (same head, effort and
    findings) collapse to one run; quick-effort runs become author-quick; named files become test."""
    repo = repo_key(repo_path)
    seen: dict[str, int] = Counter()
    for p in sorted(report_paths):
        rep = rv.load_json(p)
        ids = sorted(f["id"] for f in rep.get("findings", []))
        run_id = make_run_id(repo, rep["head_sha"], rep["effort"], [json.dumps(ids), rep.get("verdict", "")])
        seen[run_id] += 1
        if seen[run_id] > 1:
            continue
        stamp = dt.datetime.strptime(p.name[:13], "%Y%m%dT%H%M").replace(tzinfo=dt.UTC)
        m = TARGET_PR_RX.match(rep.get("target", ""))
        kind = "test" if p.name in test_names else "author-quick" if rep["effort"] == "quick" else "independent"
        led = rep.get("ledger", {})
        findings = rep.get("findings", [])
        names = [ln["name"] if isinstance(ln, dict) else ln for ln in led.get("lenses", [])]  # pilot: objects
        run = {"schema": SCHEMA, "run_id": run_id, "repo": repo, "pr": int(m.group(1)) if m else None,
               "branch": None, "target": rep.get("target"), "base_sha": None, "head_sha": rep["head_sha"],
               "kind": kind, "effort": rep["effort"], "fresh_context": rep["effort"] != "quick",
               "reviewers": led.get("reviewers"), "verdict": rep.get("verdict"), "ts": _iso(stamp),
               "counts": {s: sum(f["severity"] == s for f in findings) for s in rv.SEV},
               "evidence": {e: sum(f["evidence"] == e for f in findings) for e in rv.EVIDENCE},
               "refuter": led.get("refuter"), "suppressed": led.get("suppressed"),
               "per_lens": {n: sum(n in f["lens"].split("+") for f in findings) for n in names},
               "tokens": led.get("tokens") or {}, "cost_missing": not led.get("tokens"), "review_wall_s": None,
               "skill_version": None, "model": None, "lens_versions": {}, "intent": rep.get("intent", ""),
               "imported_from": p.name}
        if not m:
            run["branch"] = (re.match(r"^(\S+) vs ", rep.get("target", "")) or [None, None])[1]
        _write(_dir(repo, "runs") / f"{run_id}.json", run)
        rows = [{"run_id": run_id, "id": f["id"], "finding_key": finding_key(repo, f["id"]), "lens": f["lens"],
                 "severity": f["severity"], "evidence": f["evidence"], "blast": f["blast"], "file": f["file"],
                 "symbol": f.get("symbol") or "", "line": f.get("line"), "code_fp": f.get("code_fp"),
                 "summary": f["summary"], "refuter": f.get("refuter", "unrefuted")} for f in findings]
        (_dir(repo, "findings") / f"{run_id}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return {"reports": len(report_paths), "runs": len(seen), "duplicates": sum(v - 1 for v in seen.values())}
