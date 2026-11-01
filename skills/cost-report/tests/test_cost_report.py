"""Tests for cost-report: ingest, query-time pricing, rollups, hooks and the status line.

Transcripts are synthetic, shaped like Claude Code 2.1.287's (design.md section 2). Expected costs
are worked by hand from the published per-million-token rates written out in each test, never
from prices.json or the code under test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import ingest
import ledger
import rollup
import statusline

SID = "11111111-1111-1111-1111-111111111111"
SCRIPTS = Path(ledger.__file__).resolve().parent


# =========================================================================== helpers
def assistant(rid: str, ts: str, *, model: str = "claude-opus-5-5", inp: int = 2, out: int = 10,
              cr: int = 0, w5: int = 0, w1: int = 0, stop: str | None = "end_turn", sid: str = SID,
              agent: str | None = None, effort: str = "medium", speed: str = "standard",
              skill: str | None = None, web_search: int = 0) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "assistant", "requestId": rid, "timestamp": ts, "sessionId": sid, "version": "2.1.287",
        "cwd": "C:\\work\\demo", "gitBranch": "main", "entrypoint": "cli",
        "effort": effort, "perTurnEffort": effort,
        "message": {"model": model, "stop_reason": stop, "content": [], "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": w5 + w1,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
            "server_tool_use": {"web_search_requests": web_search, "web_fetch_requests": 0},
            "speed": speed}},
    }
    if agent:
        row["agentId"] = agent
        row["isSidechain"] = True
    if skill:
        row["attributionSkill"] = skill
    return row


def write_jsonl(path: Path, rows: list[dict[str, Any]], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail, encoding="utf-8")
    return path


def projects() -> Path:
    return ledger.config_dir() / "projects" / "D--demo"


def main_file(rows: list[dict[str, Any]], sid: str = SID) -> Path:
    return write_jsonl(projects() / f"{sid}.jsonl", rows)


def sub_file(agent: str, rows: list[dict[str, Any]], meta: dict[str, Any], sid: str = SID) -> Path:
    path = write_jsonl(projects() / sid / "subagents" / f"agent-{agent}.jsonl", rows)
    path.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def scan() -> dict[str, int]:
    con = ledger.connect()
    try:
        return ingest.scan(con, ingest.transcripts(ledger.config_dir() / "projects"))
    finally:
        con.close()


def report(level: str, **kw: Any) -> dict[str, Any]:
    args = argparse.Namespace(level=level, since=kw.pop("since", "all"), session=kw.pop("session", None),
                              project=kw.pop("project", None), limit=kw.pop("limit", 50))
    con = ledger.connect()
    try:
        return rollup.run(con, args)
    finally:
        con.close()


def requests() -> list[dict[str, Any]]:
    con = ledger.connect()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM requests ORDER BY ts")]
    finally:
        con.close()


def local_iso(y: int, mo: int, d: int, h: int, mi: int) -> str:
    """A local wall-clock time as the UTC ISO string Claude Code writes."""
    return dt.datetime(y, mo, d, h, mi).astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# Published rates, $/MTok (claude-api skill, 2026-09-25): written out so tests don't read prices.json.
OPUS55 = {"in": 4.0, "out": 20.0, "read": 0.20, "w5": 5.0, "w1": 8.0}
HAIKU45 = {"in": 1.0, "out": 5.0, "read": 0.10, "w5": 1.25, "w1": 2.0}


# =========================================================================== ingest
def test_streamed_rows_collapse_to_one_request_with_final_output() -> None:
    main_file([
        assistant("req_A", "2026-10-01T10:00:00.000Z", out=8, cr=36067, w1=20429, stop=None),
        assistant("req_A", "2026-10-01T10:00:01.000Z", out=156, cr=36067, w1=20429, stop="tool_use"),
    ])
    scan()
    [r] = requests()
    assert (r["output_tokens"], r["final_usage"], r["ts"]) == (156, 1, "2026-10-01T10:00:00.000Z")
    expected = (2 * OPUS55["in"] + 156 * OPUS55["out"] + 36067 * OPUS55["read"] + 20429 * OPUS55["w1"]) / 1e6
    assert report("session")["totals"]["cost_usd"] == pytest.approx(round(expected, 4))


def test_subagent_request_without_final_row_is_flagged_then_restored_from_handback() -> None:
    sub_file("agentX", [
        assistant("req_s1", "2026-10-01T10:00:00.000Z", out=21, cr=1000, stop=None, agent="agentX"),
        assistant("req_s2", "2026-10-01T10:00:05.000Z", out=3, cr=114497, stop=None, agent="agentX"),
    ], {"agentType": "general-purpose", "spawnDepth": 1, "model": "sonnet"})
    handback = {"type": "user", "sessionId": SID, "timestamp": "2026-10-01T10:00:09.000Z",
                "toolUseResult": {"agentId": "agentX", "agentType": "general-purpose",
                                  "resolvedModel": "claude-opus-5-5", "totalToolUseCount": 17,
                                  "usage": {"input_tokens": 2, "output_tokens": 2894, "cache_read_input_tokens": 114497,
                                            "output_tokens_details": {"thinking_tokens": 1297}}}}
    main_file([assistant("req_m", "2026-10-01T09:59:00.000Z"), handback])
    stats = scan()
    by_id = {r["request_id"]: r for r in requests()}
    assert stats["handbacks_applied"] == 1
    assert (by_id["req_s2"]["output_tokens"], by_id["req_s2"]["final_usage"], by_id["req_s2"]["source"]) == \
        (2894, 1, "handback")
    assert (by_id["req_s1"]["output_tokens"], by_id["req_s1"]["final_usage"]) == (21, 0)  # still a lower bound
    assert report("session")["totals"]["incomplete_output"] == 1


def test_incremental_ingest_waits_for_a_complete_last_line() -> None:
    first = json.dumps(assistant("req_1", "2026-10-01T10:00:00.000Z")) + "\n"
    second = json.dumps(assistant("req_2", "2026-10-01T10:01:00.000Z")) + "\n"
    path = write_jsonl(projects() / f"{SID}.jsonl", [], tail=first + second[:40])  # half-written line
    assert scan()["requests"] == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(second[40:])
    assert scan()["requests"] == 1
    assert scan()["unchanged"] == 1
    assert [r["request_id"] for r in requests()] == ["req_1", "req_2"]
    assert report("session")["totals"]["anomalies_ledger_wide"] == {}


def test_file_that_shrank_is_reread_from_the_start() -> None:
    path = main_file([assistant(f"req_{i}", f"2026-10-01T10:0{i}:00.000Z") for i in range(3)])
    scan()
    write_jsonl(path, [assistant("req_0", "2026-10-01T10:00:00.000Z", out=50)])
    scan()
    assert {r["request_id"]: r["output_tokens"] for r in requests()}["req_0"] == 50


def test_request_id_seen_in_two_files_is_one_request() -> None:
    main_file([assistant("req_dup", "2026-10-01T10:00:00.000Z", out=5, stop=None)])
    write_jsonl(projects() / "resumed.jsonl", [assistant("req_dup", "2026-10-01T10:00:00.500Z", out=90)])
    scan()
    [r] = requests()
    assert (r["output_tokens"], r["final_usage"]) == (90, 1)


def test_synthetic_rows_and_bad_lines_are_counted_not_priced() -> None:
    main_file([assistant("req_ok", "2026-10-01T10:00:00.000Z"),
               assistant("req_syn", "2026-10-01T10:00:01.000Z", model="<synthetic>"),
               {"type": "assistant", "sessionId": SID, "timestamp": "2026-10-01T10:00:02.000Z", "message": {}}])
    with (projects() / f"{SID}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    scan()
    assert [r["request_id"] for r in requests()] == ["req_ok"]
    assert report("session")["totals"]["anomalies_ledger_wide"] == {"synthetic": 1, "no_usage": 1, "bad_json": 1}


def test_prompt_and_response_text_never_reach_the_ledger(isolated_ledger: Path) -> None:
    row = assistant("req_1", "2026-10-01T10:00:00.000Z")
    row["message"]["content"] = [{"type": "text", "text": "RESPONSE-SECRET"}]
    main_file([{"type": "user", "sessionId": SID, "timestamp": "2026-10-01T09:59:59.000Z",
                "message": {"role": "user", "content": "PROMPT-SECRET"}}, row])
    scan()
    blob = b"".join(p.read_bytes() for p in isolated_ledger.parent.glob("ledger.db*"))
    assert b"PROMPT-SECRET" not in blob and b"RESPONSE-SECRET" not in blob


def test_concurrent_writers_lose_nothing() -> None:
    paths = [main_file([assistant(f"req_{n}_{i}", f"2026-10-01T1{n}:00:{i:02d}.000Z", sid=f"sess-{n}")
                        for i in range(40)], sid=f"sess-{n}") for n in range(4)]
    procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "ingest.py"), "file", str(p)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for p in paths]
    errors = [p.communicate()[1] for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
    assert len(requests()) == 160


# =========================================================================== pricing
def test_dated_and_1m_model_ids_are_priced() -> None:
    main_file([assistant("req_h", "2026-10-01T10:00:00.000Z", model="claude-haiku-4-5-20251001", inp=1000, out=100),
               assistant("req_o", "2026-10-01T10:00:01.000Z", model="claude-opus-5-5[1m]", inp=1000, out=100)])
    scan()
    expected = (1000 * HAIKU45["in"] + 100 * HAIKU45["out"] + 1000 * OPUS55["in"] + 100 * OPUS55["out"]) / 1e6
    totals = report("session")["totals"]
    assert totals["cost_usd"] == pytest.approx(round(expected, 4))
    assert totals["unpriced"] == 0


def test_unknown_model_is_unpriced_and_reported() -> None:
    main_file([assistant("req_ok", "2026-10-01T10:00:00.000Z", inp=1000, out=0),
               assistant("req_new", "2026-10-01T10:00:01.000Z", model="claude-future-9", inp=1000)])
    scan()
    totals = report("session")["totals"]
    assert totals["cost_usd"] == pytest.approx(round(1000 * OPUS55["in"] / 1e6, 4))
    assert (totals["unpriced"], totals["unpriced_models"]) == (1, ["claude-future-9"])


def test_price_entry_in_force_at_the_request_timestamp_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {"server_tools": {"web_search_per_request": 0.01}, "models": {"claude-x": [
        {"from": None, "input": 1.0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0},
        {"from": "2026-10-01", "input": 3.0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0},
    ]}}
    monkeypatch.setattr(ledger, "prices", lambda: table)
    main_file([assistant("req_old", "2026-09-30T23:00:00.000Z", model="claude-x", inp=1_000_000, out=0),
               assistant("req_new", "2026-10-01T00:00:00.000Z", model="claude-x", inp=1_000_000, out=0)])
    scan()
    assert report("session")["totals"]["cost_usd"] == pytest.approx(1.0 + 3.0)


def test_fast_mode_and_web_search_pricing() -> None:
    main_file([assistant("req_f", "2026-10-01T10:00:00.000Z", inp=1000, out=0, speed="fast"),
               assistant("req_w", "2026-10-01T10:00:01.000Z", inp=0, out=0, web_search=3)])
    scan()
    expected = 1000 * OPUS55["in"] * 2 / 1e6 + 3 * 10 / 1000  # fast = 2x; web search $10 per 1,000
    assert report("session")["totals"]["cost_usd"] == pytest.approx(round(expected, 4))


# =========================================================================== rollups
def test_tree_nests_subagents_and_sums_subtrees() -> None:
    main_file([assistant("req_m", "2026-10-01T10:00:00.000Z", inp=1_000_000, out=0)])            # $4
    sub_file("aaaaaaaa1", [assistant("req_a", "2026-10-01T10:01:00.000Z", inp=500_000, out=0, agent="aaaaaaaa1")],
             {"agentType": "reviewer", "spawnDepth": 1, "description": "Breadth"})                   # $2
    sub_file("bbbbbbbb2", [assistant("req_b", "2026-10-01T10:02:00.000Z", inp=250_000, out=0, agent="bbbbbbbb2")],
             {"agentType": "refuter", "spawnDepth": 2, "parentAgentId": "aaaaaaaa1"})               # $1
    scan()
    rows = report("tree", session=SID[:6])["rows"]
    assert [(r["agent"], r["self_usd"], r["subtree_usd"]) for r in rows] == [
        ("(main)", 4.0, 7.0), ("  reviewer:aaaaaaaa", 2.0, 3.0), ("    refuter:bbbbbbbb", 1.0, 1.0)]
    fan = {r["depth"]: r["cost_usd"] for r in report("fanout")["rows"]}
    assert fan == {0: 4.0, 1: 2.0, 2: 1.0}
    totals = report("session")["totals"]
    assert (totals["main_usd"], totals["subagent_usd"]) == (4.0, 3.0)


def test_day_level_splits_a_session_at_local_midnight() -> None:
    main_file([assistant("req_late", local_iso(2026, 10, 1, 23, 30), inp=1_000_000, out=0),
               assistant("req_early", local_iso(2026, 10, 2, 0, 30), inp=500_000, out=0)])
    scan()
    rows = report("day")["rows"]
    assert [(r["day"], r["cost_usd"], r["running_usd"]) for r in rows] == [
        ("2026-10-02", 2.0, 6.0), ("2026-10-01", 4.0, 4.0)]


def test_model_effort_and_skill_levels() -> None:
    main_file([assistant("req_1", "2026-10-01T10:00:00.000Z", inp=1_000_000, out=0, effort="high", skill="review-core"),
               assistant("req_2", "2026-10-01T10:00:01.000Z", inp=1_000_000, out=0, effort="medium")])
    scan()
    assert {r["model_effort"]: r["cost_usd"] for r in report("model-effort")["rows"]} == {
        "claude-opus-5-5 / high": 4.0, "claude-opus-5-5 / medium": 4.0}
    assert {r["skill"]: r["requests"] for r in report("skill")["rows"]} == {"review-core": 1, "(none)": 1}


def test_cache_view_buckets_writes_by_idle_gap() -> None:
    main_file([assistant("req_1", "2026-10-01T10:00:00.000Z", w1=1000, out=0, inp=0),
               assistant("req_2", "2026-10-01T10:02:00.000Z", cr=1000, w5=100, out=0, inp=0),
               assistant("req_3", "2026-10-01T11:30:00.000Z", w1=2000, out=0, inp=0)])
    scan()
    res = report("cache")
    [gaps] = res["sections"].values()
    assert [(g["gap_since_previous"], g["write_5m"], g["write_1h"]) for g in gaps] == [
        ("1 first call", 0, 1000), ("2 <5 min", 100, 0), ("4 >60 min", 0, 2000)]
    [row] = res["rows"]
    assert row["hit_ratio"] == pytest.approx(round(1000 / (1000 + 100 + 3000), 3))


def test_reconcile_shows_claude_codes_figure_and_the_unattributed_gap() -> None:
    main_file([assistant("req_1", "2026-10-01T10:00:00.000Z", inp=1_000_000, out=0),
               {"type": "cost-state", "sessionId": SID, "totalCostUSD": 4.5,
                "modelUsage": {"claude-opus-5-5": {"inputTokens": 1_000_000, "outputTokens": 0, "costUSD": 4.0},
                               "claude-haiku-4-5-20251001": {"inputTokens": 500_000, "costUSD": 0.5}}}])
    scan()
    [row] = report("reconcile")["rows"]
    assert (row["ledger_usd"], row["cc_usd"], row["unattributed_usd"], row["note"]) == (4.0, 4.5, 0.5, "")


def test_reconcile_compares_whole_sessions_even_when_the_window_cuts_one() -> None:
    main_file([assistant("req_old", "2026-09-01T10:00:00.000Z", inp=1_000_000, out=0),   # $4, before window
               assistant("req_new", "2026-10-01T10:00:00.000Z", inp=500_000, out=0),     # $2, inside window
               {"type": "cost-state", "sessionId": SID, "totalCostUSD": 6.0,
                "modelUsage": {"claude-opus-5-5": {"inputTokens": 1_500_000, "costUSD": 6.0}}}])
    main_file([assistant("req_x", "2026-09-01T10:00:00.000Z", sid="old-only"),
               {"type": "cost-state", "sessionId": "old-only", "totalCostUSD": 1.0,
                "modelUsage": {"claude-opus-5-5": {"costUSD": 1.0}}}], sid="old-only")
    scan()
    rows = report("reconcile", since="2026-09-15")["rows"]
    assert [(r["session_id"], r["ledger_usd"], r["cc_usd"], r["unattributed_usd"]) for r in rows] == [
        (SID, 6.0, 6.0, 0.0)]


def test_since_cutoff_forms() -> None:
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
    assert rollup.since_cutoff("24h", now) == "2026-10-02T12:00:00"
    assert rollup.since_cutoff("2w", now) == "2026-09-19T12:00:00"
    assert rollup.since_cutoff("all", now) is None
    with pytest.raises(SystemExit):
        rollup.since_cutoff("yesterday", now)


# =========================================================================== hooks and status line
def test_hook_ingests_its_session_and_never_fails(monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    path = main_file([assistant("req_m", "2026-10-01T10:00:00.000Z")])
    sub_file("agentY", [assistant("req_s", "2026-10-01T10:00:01.000Z", agent="agentY")], {"agentType": "x"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(path)})))
    assert ingest.hook(full_scan=False) == 0
    assert {r["request_id"] for r in requests()} == {"req_m", "req_s"}
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert ingest.hook(full_scan=False) == 0
    assert capsys.readouterr().out == ""
    assert "hook:" in (ledger.ledger_path().parent / "errors.log").read_text(encoding="utf-8")


def _status(pct5: float, pct7: float, cost: float = 1.25) -> dict[str, Any]:
    return {"session_id": SID, "model": {"display_name": "Opus 5.5"}, "cost": {"total_cost_usd": cost},
            "rate_limits": {"five_hour": {"used_percentage": pct5, "resets_at": 1_791_100_000},
                            "seven_day": {"used_percentage": pct7, "resets_at": 1_791_500_000}}}


def test_statusline_records_only_changes_and_renders() -> None:
    for data in (_status(10, 40), _status(10, 40), _status(12, 40)):
        statusline.record(data)
    con = ledger.connect()
    snaps = [tuple(r) for r in con.execute("SELECT limit_name, used_pct FROM limit_snapshots ORDER BY ts, limit_name")]
    cc_total = con.execute("SELECT cc_total_usd FROM sessions WHERE session_id=?", (SID,)).fetchone()[0]
    con.close()
    assert snaps == [("five_hour", 10.0), ("seven_day", 40.0), ("five_hour", 12.0)]
    assert cc_total == 1.25
    line = statusline.render(_status(12, 40))
    assert line.startswith("[Opus 5.5] · 5h 12%") and "wk 40%" in line and "session $1.25 API-eq" in line
    assert statusline.render({}) == ""


def _run_statusline(stdin: bytes) -> tuple[str, dict[str, Any]]:
    """The status line as Claude Code runs it: a subprocess fed raw bytes, read back as UTF-8."""
    out = subprocess.run([sys.executable, str(SCRIPTS / "statusline.py")], input=stdin,
                         capture_output=True, check=True).stdout.decode("utf-8").strip()
    trace = json.loads((ledger.ledger_path().parent / "statusline-last.json").read_text(encoding="utf-8"))
    return out, trace


def test_statusline_subprocess_tolerates_a_bom_and_writes_utf8_and_a_trace() -> None:
    out, trace = _run_statusline(b"\xef\xbb\xbf" + json.dumps(_status(12, 40)).encode())
    assert out.startswith("[Opus 5.5] · 5h 12%")  # the middle dot survives as UTF-8
    assert (trace["error"], trace["rate_limits"], trace["has_cost"]) == (None, ["five_hour", "seven_day"], True)
    assert "session_id" in trace["keys"] and SID not in json.dumps(trace)  # key names only, no values
    con = ledger.connect()
    assert con.execute("SELECT COUNT(*) FROM limit_snapshots").fetchone()[0] == 2
    con.close()


def test_statusline_shows_and_traces_missing_input() -> None:
    out, trace = _run_statusline(b"")
    assert out == "[cost-report: empty stdin]"
    assert (trace["stdin_bytes"], trace["error"], trace["keys"]) == (0, "empty stdin", [])


def test_limits_view_calibrates_dollars_per_percent() -> None:
    main_file([assistant("req_1", "2026-10-01T10:30:00.000Z", inp=1_000_000, out=0)])  # $4 between snapshots
    scan()
    con = ledger.connect()
    with con:
        con.executemany("INSERT INTO limit_snapshots VALUES (?,?,?,?,?,?,?)", [
            ("2026-10-01T10:00:00.000000Z", "seven_day", 20.0, "2026-10-06T09:00:00Z", None, None, SID),
            ("2026-10-01T11:00:00.000000Z", "seven_day", 22.0, "2026-10-06T09:00:00Z", None, None, SID),
        ])
    con.close()
    [row] = report("limits")["rows"]
    assert (row["limit"], row["latest_pct"], row["usd_per_pct_median"]) == ("seven_day", 22.0, 2.0)
