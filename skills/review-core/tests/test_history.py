"""Tests for scripts/history.py: run records, outcome inference, labels, bug attribution, retro,
compaction and precision. Local git repos and a local bare origin only; GitHub calls are faked."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import history as hs  # noqa: E402
import review as rv  # noqa: E402

CALC = "def calc(x):\n    return x * 2\n\n\ndef other(y):\n    return y\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, msg: str, files: dict[str, str] | None = None) -> str:
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _merge_pr(repo: Path, n: int, branch: str, msg: str, files: dict[str, str]) -> tuple[str, str]:
    """A PR as GitHub merges it: commits on a branch, then a 'Merge pull request #n' merge commit."""
    _git(repo, "checkout", "-q", "-b", branch)
    head = _commit(repo, msg, files)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from me/{branch}")
    return head, _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repo `proj` (origin: a local bare repo) with calc.py on main. GitHub calls fail, so PR numbers
    come from merge-commit messages, as they would offline."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "init", {"src/calc.py": CALC})
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))

    def no_github(args: list[str]) -> Any:
        raise rv.PostError("offline")
    monkeypatch.setattr(hs, "gh_json", no_github)
    monkeypatch.setattr(hs, "_project", lambda _repo: "me/proj")
    return repo


def _finding(**kw: Any) -> dict[str, Any]:
    base = {"id": "change-integrity:src/calc.py:calc:wrong-scale", "severity": "P1", "evidence": "TRACED",
            "blast": "MODULE", "lens": "change-integrity", "file": "src/calc.py", "line": 2, "symbol": "calc",
            "summary": "Scale is wrong.", "failure": "x", "fix": "y", "proof": "z"}
    return {**base, **kw}


def _record(repo: Path, findings: list[dict[str, Any]], *, pr: int | None = None, head: str | None = None,
            kind: str = "independent", effort: str = "standard", inputs: list[str] | None = None,
            ts: str | None = None, monkeypatch: pytest.MonkeyPatch | None = None,
            branch: str | None = "current") -> dict[str, Any]:
    """record_run as `report` calls it, with code fingerprints taken from the working tree. Without
    a PR it's a working-tree review of the current branch, unless `branch=None` (an explicit target)."""
    for f in findings:
        if fp := rv.code_fingerprint(repo / f["file"], f):
            f["code_fp"] = fp
    head = head or _git(repo, "rev-parse", "HEAD").strip()
    if branch == "current":
        branch = None if pr else _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    scope = {"repo": str(repo), "target": f"PR {pr}" if pr else "main vs main", "head_sha": head,
             "pr": {"number": pr, "merge_base": None} if pr else None, "branch": branch}
    report = {"head_sha": head, "effort": effort, "verdict": rv.verdict_of(findings), "intent": "t",
              "findings": findings,
              "ledger": {"reviewers": 1, "refuter": {"ran": False}, "suppressed": 0, "lenses": ["change-integrity"],
                         "tokens": {"reviewer": 1000, "total": 1000}}}
    if ts and monkeypatch:
        monkeypatch.setattr(hs, "_iso", lambda t=None: t.isoformat(timespec="seconds") if t else ts)

    def read(path: str) -> str | None:
        p = repo / path
        return p.read_text() if p.is_file() else None
    return hs.record_run(report, scope, [], reviewer_inputs=inputs or [json.dumps(findings)], kind=kind,
                         model="sonnet", review_wall_s=60, read_code=read)


# ------------------------------------------------------------------ run identity
def test_rerun_overwrites_and_test_runs_never_count(proj: Path, isolated_history: Path) -> None:
    r1 = _record(proj, [_finding()], pr=1)
    r2 = _record(proj, [_finding()], pr=1)                             # same review, re-run of `report`
    assert r1["run_id"] == r2["run_id"] and len(list((isolated_history / "proj" / "runs").glob("*.json"))) == 1
    _record(proj, [_finding(id="change-integrity:src/calc.py:other:t", symbol="other", line=5)], pr=2, kind="test")
    data = hs.load("proj")
    assert len(data["runs"]) == 2 and r1["repo"] == "proj" and r1["cost_missing"] is False
    rows = hs.precision("proj")
    assert rows["change-integrity"]["unlabelled"] == 1                  # the test run's finding isn't counted


# ------------------------------------------------------------------ inference between rounds
def test_inference_fixed_unlabelled_superseded_and_human_labels_stick(proj: Path) -> None:
    calc = _finding()
    other = _finding(id="change-integrity:src/calc.py:other:t", symbol="other", line=5)
    gone = _finding(id="change-integrity:src/calc.py:other:u", symbol="other", line=6, severity="P2")
    _record(proj, [calc, other, gone], pr=7, inputs=["round1"])
    (proj / "src/calc.py").write_text("def calc(x):\n    return x * 3\n\n\ndef helper(y):\n    return y\n")
    _record(proj, [], pr=7, inputs=["round2"])
    latest = hs.latest_outcomes(hs.load("proj"))
    assert latest["proj:src/calc.py:calc:wrong-scale"]["outcome"] == "fixed"
    assert latest["proj:src/calc.py:other:t"]["outcome"] == "superseded"     # `other` no longer exists
    # unchanged code, finding dropped -> unlabelled (flagged), never guessed
    _record(proj, [_finding(id="change-integrity:src/calc.py:helper:h", symbol="helper", line=5)], pr=8,
            inputs=["a"])
    hs.label(proj, "src/calc.py:helper:h", "accepted-risk", "known", by="human")
    _record(proj, [], pr=8, inputs=["b"])
    assert hs.latest_outcomes(hs.load("proj"))["proj:src/calc.py:helper:h"]["outcome"] == "accepted-risk"
    _record(proj, [_finding(id="x:src/calc.py:helper:k", symbol="helper", line=5, severity="P3")], pr=9, inputs=["c"])
    _record(proj, [], pr=9, inputs=["d"])
    assert hs.latest_outcomes(hs.load("proj"))["proj:src/calc.py:helper:k"]["outcome"] == "unlabelled"


# ------------------------------------------------------------------ labels
def test_labels_write_suppressions_and_author_may_only_dispute(proj: Path) -> None:
    _record(proj, [_finding()], pr=3)
    with pytest.raises(SystemExit, match="only mark findings 'disputed'"):
        hs.label(proj, "wrong-scale", "false-positive", "nope", by="author")
    with pytest.raises(SystemExit, match="reason is required"):
        hs.label(proj, "wrong-scale", "false-positive", " ", by="human")
    hs.label(proj, "wrong-scale", "disputed", "scale is intended", by="author")
    hs.label(proj, "wrong-scale", "false-positive", "confirmed: intended", by="human")
    sup = (proj / ".claude/review/suppressions.md").read_text()
    assert "- change-integrity:src/calc.py:calc:wrong-scale — false-positive — confirmed: intended" in sup
    events = [e for e in hs.load("proj")["outcomes"] if e["finding_key"].endswith("wrong-scale")]
    assert [e["outcome"] for e in events] == ["disputed", "false-positive"]       # history kept, latest wins
    assert len({p.name for p in (Path(hs.history_root()) / "proj" / "outcomes").iterdir()}) == 2


# ------------------------------------------------------------------ outcomes at merge
def test_merge_outcomes_suggest_and_accept(proj: Path) -> None:
    _git(proj, "checkout", "-q", "-b", "feat")
    _commit(proj, "add helper", {"src/calc.py": CALC + "\n\ndef helper(z):\n    return z\n"})
    head = _git(proj, "rev-parse", "HEAD").strip()
    _record(proj, [_finding(), _finding(id="change-integrity:src/calc.py:helper:h", symbol="helper", line=9)],
            pr=5, head=head)
    _commit(proj, "address calc", {"src/calc.py": CALC.replace("x * 2", "x * 4") + "\n\ndef helper(z):\n    return z\n"})
    _git(proj, "checkout", "-q", "main")
    _git(proj, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #5 from me/feat")
    merge = _git(proj, "rev-parse", "HEAD").strip()
    rows = hs.merge_outcomes(proj, 5, {"merged_at": "2026-10-03T00:00:00Z", "merge_commit_sha": merge})
    states = {r["finding"]["symbol"]: (r["state"], r["suggest"]) for r in rows}
    assert states == {"calc": ("changed before merge", "fixed"), "helper": ("shipped as-is", None)}
    assert hs.accept_suggestions(proj, rows, "merged") == 1
    assert hs.latest_outcomes(hs.load("proj"))["proj:src/calc.py:calc:wrong-scale"]["source"] == "human"


def test_branch_reviews_count_for_their_pr(proj: Path) -> None:
    _git(proj, "checkout", "-q", "-b", "feat-x")
    _commit(proj, "work", {"src/calc.py": CALC + "\n\ndef helper(z):\n    return z\n"})
    run = _record(proj, [_finding(id="change-integrity:src/calc.py:helper:h", symbol="helper", line=9)])
    assert run["pr"] is None and run["branch"] == "feat-x"            # an author-session branch review
    _git(proj, "checkout", "-q", "main")
    _git(proj, "merge", "-q", "--no-ff", "feat-x", "-m", "Merge pull request #6 from me/feat-x")
    merge = _git(proj, "rev-parse", "HEAD").strip()
    info = {"merged_at": "2999-01-01T00:00:00Z", "merge_commit_sha": merge, "head": {"ref": "feat-x"}}
    rows = hs.merge_outcomes(proj, 6, info)
    assert [r["state"] for r in rows] == ["shipped as-is"]
    assert hs.merge_outcomes(proj, 6, {**info, "merged_at": "2000-01-01T00:00:00Z"}) == []   # reviewed after merge


# ------------------------------------------------------------------ bug attribution
@pytest.fixture
def buggy(proj: Path) -> dict[str, Any]:
    """PR 1 introduces a bug in calc; months of unrelated work follow (a rename and a reformat listed
    in .git-blame-ignore-revs); then a fix lands on main."""
    bug_head, _ = _merge_pr(proj, 1, "feat-scale", "scale calc",
                            {"src/calc.py": CALC.replace("return x * 2", "return x * 2 + 1  # off by one")})
    _merge_pr(proj, 2, "unrelated", "docs", {"README.md": "docs\n"})
    _git(proj, "mv", "src/calc.py", "src/maths.py")
    _commit(proj, "rename calc module")
    text = (proj / "src/maths.py").read_text()
    reformat = _commit(proj, "reformat", {"src/maths.py": text.replace("def other(y):", "def other( y ):")})
    _commit(proj, "ignore the reformat in blame", {".git-blame-ignore-revs": reformat + "\n"})
    fixed = (proj / "src/maths.py").read_text().replace("x * 2 + 1  # off by one", "x * 2")
    fix = _commit(proj, "fix: calc off by one", {"src/maths.py": fixed})
    _git(proj, "push", "-q", "origin", "main")                         # on GitHub, the fix is on origin
    return {"repo": proj, "bug_head": bug_head, "fix": fix}


def test_attribution_follows_rename_and_classifies(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    cand = hs.attribute(repo, buggy["fix"])
    assert cand and cand["introduced_pr"] == 1 and cand["class"] == "unreviewed"
    assert cand["introduced_by"] == buggy["bug_head"] and "src/calc.py" in cand["files"]   # old name kept
    _record(repo, [_finding(id="change-integrity:src/calc.py:other:o", symbol="other", line=5, severity="P2")],
            pr=1, head=buggy["bug_head"])
    assert hs.attribute(repo, buggy["fix"])["class"] == "missed"         # reviewed; nothing on calc
    _record(repo, [_finding()], pr=1, head=buggy["bug_head"], inputs=["second review"])
    hs.label(repo, "wrong-scale", "false-positive", "looked fine", by="human")
    cand = hs.attribute(repo, buggy["fix"])
    assert cand["class"] == "dismissed-real-bug" and cand["covering"] == ["change-integrity:src/calc.py:calc:wrong-scale"]
    with pytest.raises(SystemExit, match="provisional"):
        hs.confirm_escape(repo, cand)                                  # same function only: a human decides
    hs.confirm_escape(repo, cand, klass="dismissed-real-bug")
    data = hs.load("proj")
    assert hs.latest_outcomes(data)["proj:src/calc.py:calc:wrong-scale"]["outcome"] == "dismissed-real-bug"
    assert data["escapes"][0]["class"] == "dismissed-real-bug" and data["escapes"][0]["confirmed_by"] == "human"


def test_attribution_accepted_risk_and_same_pr_fix(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    _record(repo, [_finding()], pr=1, head=buggy["bug_head"])
    hs.label(repo, "wrong-scale", "accepted-risk", "edge case", by="human")
    assert hs.attribute(repo, buggy["fix"])["class"] == "accepted-materialised"
    # a bug introduced and fixed inside one PR is not an escape
    _git(repo, "checkout", "-q", "-b", "inner")
    _commit(repo, "add buggy g", {"src/g.py": "def g():\n    return 1 / 0\n"})
    inner_fix = _commit(repo, "fix g", {"src/g.py": "def g():\n    return 1\n"})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "inner", "-m", "Merge pull request #9 from me/inner")
    _git(repo, "push", "-q", "origin", "main")
    assert hs.attribute(repo, inner_fix) is None


def test_add_only_fix_blames_enclosing_function_at_low_confidence(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    text = (repo / "src/maths.py").read_text().replace("def calc(x):\n", "def calc(x):\n    assert x >= 0\n")
    fix = _commit(repo, "fix: guard negative input", {"src/maths.py": text})
    cand = hs.attribute(repo, fix)
    assert cand and cand["confidence"] == "low" and "src/maths.py::calc" in cand["symbols"]


def test_manual_escape_for_a_bug_without_fix(proj: Path) -> None:
    _record(proj, [_finding()], pr=4)
    cand = hs.manual_escape(proj, "src/calc.py", "calc", "prod shows wrong totals", 4)
    assert cand["class"] == "flagged-unlabelled" and cand["covering"] and cand["provisional"]
    assert hs.manual_escape(proj, "src/calc.py", "other", "x", 4)["class"] == "missed"
    assert hs.manual_escape(proj, "src/calc.py", "calc", "x", None)["class"] == "unreviewed"


# ------------------------------------------------------------------ retro
def test_retro_stats_candidates_and_unlabelled(buggy: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    repo = buggy["repo"]
    _record(repo, [_finding()], pr=1, head=buggy["bug_head"])
    merged = [{"number": 1, "title": "scale calc", "mergedAt": dt.datetime.now(dt.UTC).isoformat(),
               "createdAt": "2026-01-01T00:00:00Z", "labels": []}]
    r = hs.retro(repo, since=(dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%d"), merged=merged)
    pr1 = r["prs"][0]
    assert pr1["reviews"] == 1 and pr1["p0_p1_at_merge"] == ["change-integrity:src/calc.py:calc:wrong-scale"]
    assert pr1["unlabelled"] and pr1["tokens"] == 1000
    assert [c["fix"] for c in r["escape_candidates"]] == [buggy["fix"]]
    assert r["escape_candidates"][0]["class"] == "flagged-unlabelled"     # same function, never labelled
    assert (Path(hs.history_root()) / "proj" / "retro" / f"{r['period']}.json").exists()
    with pytest.raises(SystemExit, match="provisional"):
        hs.confirm_escape(repo, r["escape_candidates"][0])                # provisional: a human decides
    hs.confirm_escape(repo, r["escape_candidates"][0], klass="dismissed-real-bug")
    assert hs.latest_outcomes(hs.load("proj"))["proj:src/calc.py:calc:wrong-scale"]["outcome"] == "dismissed-real-bug"
    again = hs.retro(repo, since=(dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
                     merged=merged)
    assert again["escape_candidates"] == []                                 # confirmed fixes aren't re-listed
    assert "Bug attribution candidates" in hs.render_retro(r) and "(provisional)" in hs.render_retro(r)


def test_not_a_bug_is_recorded_and_never_counted(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    _record(repo, [_finding()], pr=1, head=buggy["bug_head"])
    hs.confirm_escape(repo, hs.attribute(repo, buggy["fix"]), klass="not-a-bug", note="a refactor, not a bug")
    data = hs.load("proj")
    assert data["escapes"][0]["class"] == "not-a-bug"
    assert hs.latest_outcomes(data).get("proj:src/calc.py:calc:wrong-scale") is None    # outcome untouched
    assert hs.precision("proj")["change-integrity"]["escapes"] == {}


def test_retro_assigns_branch_reviews_to_one_pr(proj: Path) -> None:
    _git(proj, "checkout", "-q", "-b", "shared")
    _commit(proj, "w1", {"src/a.py": "def a():\n    return 1\n"})
    run = _record(proj, [_finding(id="x:src/a.py:a:one", file="src/a.py", symbol="a", line=2)], inputs=["b1"])
    _git(proj, "checkout", "-q", "main")
    now = dt.datetime.now(dt.UTC)
    _set_ts(run["run_id"], (now - dt.timedelta(hours=3)).isoformat(timespec="seconds"))
    merged = [{"number": 1, "title": "first", "mergedAt": (now - dt.timedelta(hours=2)).isoformat(),
               "createdAt": "x", "labels": [], "headRefName": "shared"},
              {"number": 3, "title": "second", "mergedAt": (now - dt.timedelta(hours=1)).isoformat(),
               "createdAt": "x", "labels": [], "headRefName": "shared"}]
    r = hs.retro(proj, since=(now - dt.timedelta(days=1)).strftime("%Y-%m-%d"), merged=merged)
    reviews = {p["pr"]: p["reviews"] for p in r["prs"]}
    assert reviews == {1: 1, 3: 0}                                       # not counted twice


# ------------------------------------------------------------------ compaction
def _set_ts(run_id: str, ts: str) -> None:
    p = Path(hs.history_root()) / "proj" / "runs" / f"{run_id}.json"
    rec = json.loads(p.read_text())
    rec["ts"] = ts
    p.write_text(json.dumps(rec))


def test_compaction_is_lossless_idempotent_and_expires_text(proj: Path) -> None:
    run = _record(proj, [_finding()], pr=1)
    _set_ts(run["run_id"], "2026-01-15T00:00:00+00:00")
    ev = hs.label(proj, "wrong-scale", "fixed", "done", by="human")
    before = hs.load("proj")
    agg_before = hs.aggregates("proj")
    now = dt.datetime(2026, 4, 2, tzinfo=dt.UTC)                     # Jan is closed; Oct events stay hot
    out = hs.compact("proj", now=now)
    base = Path(hs.history_root()) / "proj"
    assert out["archived"] == ["2026-01"] and not list((base / "runs").glob("*.json"))
    assert (base / "outcomes" / next(p.name for p in (base / "outcomes").iterdir())).exists()  # this month's event
    after = hs.load("proj")
    assert after["runs"] == before["runs"] and after["findings"] == before["findings"]
    assert hs.aggregates("proj")["months"] == agg_before["months"]
    assert hs.compact("proj", now=now)["archived"] == []                    # idempotent
    late = dt.datetime(2027, 3, 1, tzinfo=dt.UTC)                           # Jan 2026 is now > 12 months old
    assert hs.compact("proj", now=late)["expired"] == ["2026-01"]
    with gzip.open(base / "archive" / "2026-01.jsonl.gz", "rt", encoding="utf-8") as fh:
        recs = [json.loads(ln) for ln in fh]
    finding = next(e for e in recs if e["type"] == "findings")["rec"][0]
    assert "summary" not in finding and finding["symbol"] == "calc" and finding["code_fp"]
    assert hs.latest_outcomes(hs.load("proj"))[ev["finding_key"]]["outcome"] == "fixed"
    assert hs.compact("proj", now=late)["expired"] == []


def test_attribution_works_from_expired_history(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    run = _record(repo, [_finding()], pr=1, head=buggy["bug_head"])
    _set_ts(run["run_id"], "2025-01-10T00:00:00+00:00")
    hs.compact("proj", now=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))      # archived and expired
    assert hs.attribute(repo, buggy["fix"])["class"] == "flagged-unlabelled"


# ------------------------------------------------------------------ precision
def test_wilson_and_precision_windows(proj: Path) -> None:
    assert hs.wilson_lower(0, 0) is None
    assert round(hs.wilson_lower(8, 8), 3) == 0.676                          # 8/8 is not "trustworthy"
    assert round(hs.wilson_lower(38, 40), 3) == 0.835
    for i, out in enumerate(["fixed", "fixed", "false-positive", "accepted-risk"]):
        f = _finding(id=f"change-integrity:src/calc.py:calc:s{i}")
        _record(proj, [f], pr=10 + i)
        hs.label(proj, f"calc:s{i}", out, "r", by="human")
    _record(proj, [_finding(id="change-integrity:src/calc.py:calc:open")], pr=20)
    row = hs.precision("proj")["change-integrity"]
    assert (row["tp"], row["fp"], row["unlabelled"], row["precision"], row["sufficient"]) == (3, 1, 1, 0.75, False)
    assert row["prs"] == 5
    old = _record(proj, [_finding(id="change-integrity:src/calc.py:calc:old")], pr=30)
    _set_ts(old["run_id"], "2024-01-01T00:00:00+00:00")
    assert hs.precision("proj", months=12)["change-integrity"]["unlabelled"] == 1     # 2024 run outside window
    assert hs.precision("proj", months=None)["change-integrity"]["unlabelled"] == 2
    text = hs.render_precision(hs.precision("proj"), "proj")
    assert "insufficient" in text and "Wilson" in text


def test_since_lens_change_uses_only_current_lens_version(proj: Path) -> None:
    a = _record(proj, [_finding(id="change-integrity:src/calc.py:calc:a")], pr=1)
    b = _record(proj, [_finding(id="change-integrity:src/calc.py:calc:b")], pr=2)
    for run, version in ((a, "v1"), (b, "v2")):
        p = Path(hs.history_root()) / "proj" / "runs" / f"{run['run_id']}.json"
        rec = json.loads(p.read_text())
        rec["lens_versions"] = {"change-integrity": version}
        rec["ts"] = "2026-10-01T00:00:00+00:00" if version == "v1" else "2026-10-02T00:00:00+00:00"
        p.write_text(json.dumps(rec))
    assert hs.precision("proj", since_lens_change=True)["change-integrity"]["unlabelled"] == 1


# ------------------------------------------------------------------ backfill
def test_import_dedupes_and_classifies(proj: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    rep = {"schema": "review-core/1", "target": "PR 5 (main..abc1234)", "head_sha": "a" * 40, "effort": "standard",
           "verdict": "FIX-FIRST", "findings": [_finding()],
           "ledger": {"reviewers": 1, "lenses": ["change-integrity"], "tokens": {}}}
    for name in ("20261003T1613-abc1234.json", "20261003T1615-abc1234.json", "20261003T1616-abc1234.json"):
        (out / name).write_text(json.dumps(rep))
    (out / "20261003T1700-bbb.json").write_text(json.dumps({**rep, "effort": "quick", "target": "feat vs main"}))
    (out / "20261003T1800-ccc.json").write_text(json.dumps({**rep, "head_sha": "c" * 40}))
    res = hs.import_reports(proj, sorted(out.glob("*.json")), {"20261003T1800-ccc.json"})
    assert res == {"reports": 5, "runs": 3, "duplicates": 2}
    kinds = sorted((r["kind"], r["pr"]) for r in hs.load("proj")["runs"].values())
    assert kinds == [("author-quick", None), ("independent", 5), ("test", 5)]
    assert hs.import_reports(proj, sorted(out.glob("*.json")), {"20261003T1800-ccc.json"})["runs"] == 3  # idempotent
    assert len(hs.load("proj")["runs"]) == 3


# ------------------------------------------------------------------ report integration
def test_report_records_history_and_kind(tmp_path: Path, isolated_history: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "init", {"src/calc.py": CALC})
    (repo / "src/calc.py").write_text(CALC.replace("x * 2", "x * 5"))
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate"])
    rv.main(["start", "--work", str(work)])
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": [_finding()], "checked": [], "not_checked": []}))
    for _ in range(2):                                                   # dry run, then the real report
        rv.main(["report", "--work", str(work), "--findings", str(f), "--cost", "reviewer=900"])
    capsys.readouterr()
    runs = list((isolated_history / "r" / "runs").glob("*.json"))
    rec = json.loads(runs[0].read_text())
    assert len(runs) == 1 and rec["kind"] == "independent" and rec["tokens"]["total"] == 900
    assert rec["review_wall_s"] is not None and rec["skill_version"] and rec["model"] == "sonnet"
    latest = json.loads((repo / ".claude/review/out/latest.json").read_text())
    assert latest["schema"] == "review-core/2" and latest["repo"] == "r" and latest["run_id"] == rec["run_id"]
    rv.main(["report", "--work", str(work), "--findings", str(f), "--kind", "test", "--no-history"])
    assert len(list((isolated_history / "r" / "runs").glob("*.json"))) == 1


# ------------------------------------------------------------------ fixes from the outcomes review
def test_outcome_names_are_short_and_old_names_migrate(proj: Path, isolated_history: Path) -> None:
    long_id = "change-integrity:skills/review-core/scripts/review.py:post_review:" + "x" * 60
    _record(proj, [_finding(id=long_id)], pr=1)
    hs.label(proj, long_id, "fixed", "done", by="human")
    d = isolated_history / "proj" / "outcomes"
    name = next(d.iterdir()).name
    assert len(f"review-history/claudeconfig/outcomes/{name}") <= 115           # was ~154
    ev = json.loads((d / name).read_text())
    old = d / f"{'y' * 80}-{hs._h(ev['finding_key'], 8)}.{'2' * 18}0001.json"   # the old ~117-char form
    (d / name).rename(old)
    assert hs.compact("proj")["renamed"] == 1 and (d / name).exists() and not old.exists()
    assert hs.compact("proj")["renamed"] == 0                                    # idempotent
    assert hs.latest_outcomes(hs.load("proj"))[ev["finding_key"]]["outcome"] == "fixed"


def test_explicit_targets_never_chain_as_rounds(proj: Path) -> None:
    _commit(proj, "second", {"src/b.py": "x = 1\n"})
    assert rv.resolve_scope(proj, "HEAD", False)["branch"] is None              # a commit target
    _record(proj, [_finding()], branch=None, inputs=["commit X"])
    (proj / "src/calc.py").write_text(CALC.replace("x * 2", "x * 9"))
    _record(proj, [], branch=None, inputs=["range A..B"])                       # an unrelated review
    assert hs.load("proj")["outcomes"] == []                                    # nothing inferred


def test_escape_pr_override_recomputes_class(buggy: dict[str, Any]) -> None:
    repo = buggy["repo"]
    _record(repo, [_finding()], pr=1, head=buggy["bug_head"])
    hs.label(repo, "wrong-scale", "false-positive", "x", by="human")
    blamed = hs.attribute(repo, buggy["fix"])
    assert blamed["introduced_pr"] == 1 and blamed["covering"]
    moved = hs.attribute(repo, buggy["fix"], intro_pr=9)                         # human: it came from PR 9
    assert moved["introduced_pr"] == 9 and moved["class"] == "unreviewed" and moved["covering"] == []


def test_merge_outcomes_refuses_a_missing_merge_commit(proj: Path) -> None:
    _record(proj, [_finding()], pr=5)
    with pytest.raises(SystemExit, match="not available locally"):
        hs.merge_outcomes(proj, 5, {"merged_at": "2026-10-03T00:00:00Z", "merge_commit_sha": "d" * 40})


def test_report_survives_a_broken_history(tmp_path: Path, isolated_history: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "init", {"src/calc.py": CALC})
    (repo / "src/calc.py").write_text(CALC.replace("x * 2", "x * 5"))
    bad = isolated_history / "r" / "runs"
    bad.mkdir(parents=True)
    (bad / "broken.json").write_text("<<<<<<< HEAD\n{")                       # a half-merged file
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate"])
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": [_finding()], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(f), "--cost", "reviewer=1"])
    err = capsys.readouterr().err
    latest = json.loads((repo / ".claude/review/out/latest.json").read_text())
    assert "review history NOT recorded" in err and latest["findings"] and "run_id" not in latest


# ------------------------------------------------------------------ phase A housekeeping
def test_one_run_per_prep_even_with_extra_findings_files(tmp_path: Path, isolated_history: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "init", {"src/calc.py": CALC})
    (repo / "src/calc.py").write_text(CALC.replace("x * 2", "x * 5"))
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate"])
    f1, f2 = tmp_path / "reviewer.json", tmp_path / "redteam.json"
    f1.write_text(json.dumps({"findings": [_finding()], "checked": [], "not_checked": []}))
    f2.write_text(json.dumps({"findings": [], "checked": ["proof tests"], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(f1), "--cost", "reviewer=1"])
    rv.main(["report", "--work", str(work), "--findings", str(f1), "--findings", str(f2), "--cost", "reviewer=1"])
    capsys.readouterr()
    runs = list((isolated_history / "r" / "runs").glob("*.json"))
    assert len(runs) == 1 and json.loads(runs[0].read_text())["prepared_at"]


def test_compact_merges_duplicate_runs_of_one_review(proj: Path, isolated_history: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    a = _record(proj, [_finding()], pr=9, inputs=["reviewer"], ts="2026-10-02T10:00:00+00:00",
                monkeypatch=monkeypatch)
    b = _record(proj, [_finding()], pr=9, inputs=["reviewer", "red team"],     # the old duplicate pattern
                ts="2026-10-02T10:00:01+00:00", monkeypatch=monkeypatch)
    _record(proj, [_finding(id="change-integrity:src/calc.py:other:o", symbol="other", line=5)], pr=9,
            inputs=["a later round"])
    assert a["run_id"] != b["run_id"] and len(hs.load("proj")["runs"]) == 3
    out = hs.compact("proj")
    assert out["deduped"] == [b["run_id"]] and len(hs.load("proj")["runs"]) == 2
    assert hs.compact("proj")["deduped"] == []                                   # idempotent
