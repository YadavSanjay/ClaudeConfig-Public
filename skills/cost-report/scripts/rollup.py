"""cost-report rollup: aggregate the ledger at one level. Design: ../design.md (section 6).

    rollup.py --level LEVEL [--since 7d|24h|2w|YYYY-MM-DD|all] [--session ID] [--project NAME]
              [--limit N] [--format table|json] [--no-ingest]

Levels:
    session       per session: total, main thread vs subagents, models
    tree          one session's agent tree (needs --session; a prefix is enough): self and subtree cost
    agent-type    per subagent type ('(main)' = main threads): runs, cost, cost per run
    model-effort  per model x effort: requests, cost per request
    day           per local calendar day (each call by its own timestamp), with a running total
    skill         per skill active at call time ('(none)' outside skills)
    project       per project (last component of the session's cwd)
    cache         per model: hit ratio, 5m vs 1h writes, where the money goes; writes by idle gap
    fanout        spend by agent depth; sessions with the largest subagent spend
    limits        recorded plan-limit snapshots; API-equivalent $ per 1% of each limit
    reconcile     ledger vs Claude Code's own session cost; unattributed gap

Costs are API list prices computed at query time (prices.json); on a subscription they are an
API-equivalent figure, not a bill. Before aggregating, new transcript lines are ingested
(skip with --no-ingest). The model presents this output and never recomputes it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Any

import ingest
import ledger

LEVELS = ("session", "tree", "agent-type", "model-effort", "day", "skill", "project",
          "cache", "fanout", "limits", "reconcile")
SINCE_RX = re.compile(r"^(\d+)([hdw])$")
MAIN = "(main)"
COST = ledger.COST_SQL

TOKENS = f"""COUNT(*) AS requests,
  SUM(r.input_tokens) AS input_tokens, SUM(r.output_tokens) AS output_tokens,
  SUM(r.cache_read) AS cache_read, SUM(r.cache_write_5m + r.cache_write_1h) AS cache_write,
  ROUND(COALESCE(SUM({COST}), 0), 4) AS cost_usd"""
SPLIT = f""",
  ROUND(COALESCE(SUM(CASE WHEN r.agent_id IS NULL THEN {COST} END), 0), 4) AS main_usd,
  ROUND(COALESCE(SUM(CASE WHEN r.agent_id IS NOT NULL THEN {COST} END), 0), 4) AS subagent_usd"""
FROM = """FROM requests r
  LEFT JOIN agents a ON a.agent_id = r.agent_id
  LEFT JOIN sessions s ON s.session_id = r.session_id"""


def since_cutoff(since: str, now: dt.datetime | None = None) -> str | None:
    """ISO UTC lower bound for ts, or None for 'all'. A date means local midnight of that day."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if since == "all":
        return None
    if m := SINCE_RX.match(since):
        n, unit = int(m[1]), m[2]
        delta = {"h": dt.timedelta(hours=n), "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[unit]
        return (now - delta).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        day = dt.date.fromisoformat(since)
    except ValueError as exc:
        raise SystemExit(f"--since: expected Nh, Nd, Nw, YYYY-MM-DD or 'all', got {since!r}") from exc
    local_midnight = dt.datetime.combine(day, dt.time()).astimezone()
    return local_midnight.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _where(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if cutoff := since_cutoff(args.since):
        clauses.append("r.ts >= ?")
        params.append(cutoff)
    if args.session:
        clauses.append("r.session_id LIKE ?")
        params.append(args.session + "%")
    if args.project:
        clauses.append("s.project = ?")
        params.append(args.project)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _query(con: sqlite3.Connection, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params)]


def by_column(con: sqlite3.Connection, key_sql: str, key: str, where: str, params: list[Any],
              limit: int, order: str = "cost_usd DESC", extra: str = "") -> list[dict[str, Any]]:
    sql = f"SELECT {key_sql} AS {key}, {TOKENS}{extra} {FROM} {where} GROUP BY 1 ORDER BY {order} LIMIT ?"
    return _query(con, sql, [*params, limit])


# =========================================================================== levels
def by_session(con: sqlite3.Connection, where: str, params: list[Any], limit: int,
               order: str = "cost_usd DESC") -> list[dict[str, Any]]:
    sql = f"""SELECT r.session_id, s.project, s.title, MIN(r.ts) AS first_ts, {TOKENS}{SPLIT},
        COUNT(DISTINCT r.agent_id) AS subagents, GROUP_CONCAT(DISTINCT r.model) AS models
      {FROM} {where} GROUP BY r.session_id ORDER BY {order} LIMIT ?"""
    return _query(con, sql, [*params, limit])


def by_day(con: sqlite3.Connection, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
    extra = SPLIT + ", COUNT(DISTINCT r.session_id) AS sessions"
    rows = by_column(con, "date(r.ts, 'localtime')", "day", where, params, limit, order="day DESC", extra=extra)
    running = 0.0
    for row in reversed(rows):
        running += row["cost_usd"]
        row["running_usd"] = round(running, 4)
    return rows


def resolve_session(con: sqlite3.Connection, prefix: str) -> str:
    sids = [r[0] for r in con.execute(
        "SELECT DISTINCT session_id FROM requests WHERE session_id LIKE ?", (prefix + "%",))]
    if len(sids) != 1:
        raise SystemExit(f"--session {prefix!r} matches {len(sids)} sessions; give a longer prefix")
    return str(sids[0])


def tree(con: sqlite3.Connection, prefix: str) -> list[dict[str, Any]]:
    """The session's agent tree, depth-first, with self and subtree (self + descendants) cost."""
    sid = resolve_session(con, prefix)
    own = {row["agent_id"]: row for row in _query(con, f"""
        SELECT r.agent_id, {TOKENS}, GROUP_CONCAT(DISTINCT r.model) AS models,
               GROUP_CONCAT(DISTINCT r.effort) AS efforts, SUM(1 - r.final_usage) AS incomplete
        FROM requests r WHERE r.session_id = ? GROUP BY r.agent_id""", [sid])}
    meta = {row["agent_id"]: row for row in _query(con, "SELECT * FROM agents WHERE session_id = ?", [sid])}
    known = set(own) | set(meta)
    children: dict[str | None, list[str]] = defaultdict(list)
    for aid in known - {None}:
        parent = (meta.get(aid) or {}).get("parent_agent_id")
        children[parent if parent in known else None].append(aid)

    def subtree(aid: str | None) -> float:
        return float((own.get(aid) or {}).get("cost_usd") or 0.0) + sum(subtree(c) for c in children[aid])

    out: list[dict[str, Any]] = []

    def walk(aid: str | None, depth: int) -> None:
        o, m = own.get(aid) or {}, (meta.get(aid) or {}) if aid else {}
        out.append({
            "agent": "  " * depth + (MAIN if aid is None else f"{m.get('agent_type') or '?'}:{aid[:8]}"),
            "description": m.get("description") or "",
            "models": o.get("models"), "efforts": o.get("efforts"), "requests": o.get("requests", 0),
            "incomplete": o.get("incomplete", 0),
            "self_usd": round(float(o.get("cost_usd") or 0.0), 4), "subtree_usd": round(subtree(aid), 4),
        })
        for c in sorted(children[aid], key=subtree, reverse=True):
            walk(c, depth + 1)

    walk(None, 0)
    return out


def cache(con: sqlite3.Connection, where: str, params: list[Any], limit: int) -> dict[str, Any]:
    part = "cost(r.model, r.ts, r.speed, {}, {}, {}, {}, {}, 0)"
    rows = _query(con, f"""SELECT r.model, COUNT(*) AS requests,
          SUM(r.input_tokens) AS input_tokens, SUM(r.cache_read) AS cache_read,
          SUM(r.cache_write_5m) AS write_5m, SUM(r.cache_write_1h) AS write_1h,
          ROUND(1.0 * SUM(r.cache_read) / NULLIF(SUM(r.input_tokens + r.cache_read + r.cache_write_5m
                + r.cache_write_1h), 0), 3) AS hit_ratio,
          ROUND(SUM({part.format('r.input_tokens', 0, 0, 0, 0)}), 4) AS input_usd,
          ROUND(SUM({part.format(0, 0, 'r.cache_read', 0, 0)}), 4) AS read_usd,
          ROUND(SUM({part.format(0, 0, 0, 'r.cache_write_5m', 'r.cache_write_1h')}), 4) AS write_usd,
          ROUND(SUM({part.format(0, 'r.output_tokens', 0, 0, 0)}), 4) AS output_usd
        {FROM} {where} GROUP BY r.model ORDER BY write_usd + read_usd DESC LIMIT ?""", [*params, limit])
    gaps = _query(con, f"""WITH g AS (
          SELECT r.*, (julianday(r.ts) - julianday(LAG(r.ts) OVER (
              PARTITION BY r.session_id, COALESCE(r.agent_id, '') ORDER BY r.ts))) * 1440 AS gap_min
          {FROM} {where})
        SELECT CASE WHEN gap_min IS NULL THEN '1 first call' WHEN gap_min < 5 THEN '2 <5 min'
                    WHEN gap_min < 60 THEN '3 5-60 min' ELSE '4 >60 min' END AS gap_since_previous,
               COUNT(*) AS requests, SUM(cache_write_5m) AS write_5m, SUM(cache_write_1h) AS write_1h,
               ROUND(SUM(cost(model, ts, speed, 0, 0, 0, cache_write_5m, cache_write_1h, 0)), 4) AS write_usd
        FROM g r GROUP BY 1 ORDER BY 1""", params)
    return {"rows": rows, "sections": {"cache writes by idle gap (5m TTL expires >5 min, 1h >60 min)": gaps}}


def fanout(con: sqlite3.Connection, where: str, params: list[Any], limit: int) -> dict[str, Any]:
    rows = by_column(con, "COALESCE(a.spawn_depth, CASE WHEN r.agent_id IS NULL THEN 0 ELSE 1 END)", "depth",
                     where, params, limit, order="depth",
                     extra=", COUNT(DISTINCT COALESCE(r.agent_id, r.session_id)) AS agents")
    top = by_session(con, where, params, limit, order="subagent_usd DESC")
    for row in top:
        row["subagent_share"] = round(row["subagent_usd"] / row["cost_usd"], 3) if row["cost_usd"] else None
    keep = ("session_id", "title", "cost_usd", "subagent_usd", "subagent_share", "subagents")
    return {"rows": rows, "sections": {"sessions by subagent spend": [
        {k: r[k] for k in keep} for r in top if r["subagent_usd"]]}}


def limits(con: sqlite3.Connection, args: argparse.Namespace, limit: int) -> dict[str, Any]:
    cutoff = since_cutoff(args.since)
    snaps = _query(con, "SELECT * FROM limit_snapshots WHERE ts >= ? ORDER BY limit_name, ts",
                   [cutoff or ""])
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in snaps:
        by_name[s["limit_name"]].append(s)
    rows = []
    for name, series in by_name.items():
        rates = []
        for a, b in zip(series, series[1:]):
            dpct = (b["used_pct"] or 0) - (a["used_pct"] or 0)
            if a["resets_at"] != b["resets_at"] or dpct < 1:
                continue
            usd = con.execute(f"SELECT COALESCE(SUM({COST}), 0) FROM requests r WHERE r.ts > ? AND r.ts <= ?",
                              (a["ts"][:19], b["ts"][:19])).fetchone()[0]
            rates.append(usd / dpct)
        last = series[-1]
        rows.append({
            "limit": name, "snapshots": len(series), "latest_pct": last["used_pct"],
            "resets_at": last["resets_at"], "latest_at": last["ts"][:19],
            "usd_per_pct_median": round(statistics.median(rates), 4) if rates else None,
            "rate_samples": len(rates),
        })
    history = [{k: s[k] for k in ("ts", "limit_name", "used_pct", "resets_at", "used_usd", "limit_usd")}
               for s in snaps[-limit:]]
    note = [] if snaps else [{"note": "no snapshots yet: enable statusline.py (SKILL.md) in an interactive session"}]
    return {"rows": rows or note, "sections": {"recent snapshots": history}}


def reconcile(con: sqlite3.Connection, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
    """Claude Code's figures cover whole sessions, so the window only picks sessions active in it;
    the ledger side is always summed over the whole session too."""
    rows = _query(con, f"""SELECT r.session_id, s.title, ROUND(COALESCE(SUM({COST}), 0), 4) AS ledger_usd,
          SUM(1 - r.final_usage) AS incomplete, s.cc_total_usd,
          (SELECT ROUND(SUM(c.cost_usd), 4) FROM cc_reported c WHERE c.session_id = r.session_id) AS cc_state_usd
        FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
        WHERE r.session_id IN (SELECT r.session_id {FROM} {where}) GROUP BY r.session_id
        HAVING s.cc_total_usd IS NOT NULL OR cc_state_usd IS NOT NULL
        ORDER BY ledger_usd DESC LIMIT ?""", [*params, limit])
    for row in rows:
        cc = row["cc_total_usd"] if row["cc_total_usd"] is not None else row["cc_state_usd"]
        row["cc_usd"] = round(cc, 4)
        row["unattributed_usd"] = round(cc - row["ledger_usd"], 4)
        row["note"] = "stale (session continued after Claude Code's figure)" if row["unattributed_usd"] < -0.0005 else ""
    return rows


def totals(con: sqlite3.Connection, where: str, params: list[Any]) -> dict[str, Any]:
    row = con.execute(f"""SELECT {TOKENS}{SPLIT}, SUM({COST} IS NULL) AS unpriced,
        SUM(1 - r.final_usage) AS incomplete_output, COUNT(DISTINCT r.session_id) AS sessions,
        GROUP_CONCAT(DISTINCT r.model) AS models {FROM} {where}""", params).fetchone()
    out = dict(row)
    models = [m for m in (out.pop("models") or "").split(",") if m]
    out["unpriced_models"] = sorted(m for m in models if ledger.verification(m) is None)
    out["assumed_price_models"] = sorted(m for m in models if ledger.verification(m) == "assumed")
    billed = sum(out[k] or 0 for k in ("input_tokens", "cache_read", "cache_write"))
    out["cache_read_share"] = round((out["cache_read"] or 0) / billed, 3) if billed else None
    out["anomalies_ledger_wide"] = {r[0]: r[1] for r in con.execute(
        "SELECT kind, SUM(count) FROM anomalies GROUP BY kind")}
    return out


def run(con: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    where, params = _where(args)
    level, limit = args.level, args.limit
    sections: dict[str, Any] = {}
    if level == "session":
        rows = by_session(con, where, params, limit)
    elif level == "tree":
        if not args.session:
            raise SystemExit("--level tree needs --session ID (a prefix is enough)")
        rows = tree(con, args.session)
    elif level == "agent-type":
        runs = "COUNT(DISTINCT COALESCE(r.agent_id, r.session_id))"
        rows = by_column(con, f"COALESCE(a.agent_type, CASE WHEN r.agent_id IS NULL THEN '{MAIN}' ELSE '?' END)",
                         "agent_type", where, params, limit,
                         extra=f", {runs} AS runs, ROUND(COALESCE(SUM({COST}), 0) / {runs}, 4) AS usd_per_run")
    elif level == "model-effort":
        rows = by_column(con, "r.model || ' / ' || COALESCE(r.effort, '?')", "model_effort", where, params,
                         limit, extra=f", ROUND(COALESCE(SUM({COST}), 0) / COUNT(*), 5) AS usd_per_request")
    elif level == "day":
        rows = by_day(con, where, params, limit)
    elif level == "skill":
        rows = by_column(con, "COALESCE(r.skill, '(none)')", "skill", where, params, limit)
    elif level == "project":
        rows = by_column(con, "COALESCE(s.project, '?')", "project", where, params, limit)
    elif level == "reconcile":
        rows = reconcile(con, where, params, limit)
    else:
        res = {"cache": cache, "fanout": fanout}[level](con, where, params, limit) if level != "limits" \
            else limits(con, args, limit)
        rows, sections = res["rows"], res["sections"]
    return {
        "level": level, "since": args.since, "session": args.session, "project": args.project,
        "ledger": str(ledger.ledger_path()), "cost_basis": "API list price (API-equivalent on a subscription)",
        "totals": totals(con, where, params), "rows": rows, "sections": sections,
    }


# =========================================================================== output
def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _grid(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["(no rows)"]
    cols = list(rows[0])
    cells = [[_fmt(r.get(c)) for c in cols] for r in rows]
    width = [min(44, max(len(c), *(len(x[i]) for x in cells))) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, width))]
    lines += ["  ".join(x[:w].ljust(w) for x, w in zip(row, width)) for row in cells]
    return lines


def table(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [f"level={report['level']}  since={report['since']}  sessions={t['sessions']}  "
             f"total=${t['cost_usd'] or 0:,.2f} (main ${t['main_usd'] or 0:,.2f} / subagents "
             f"${t['subagent_usd'] or 0:,.2f})  cache-read share={t['cache_read_share']}  [API list price]"]
    lines += _grid(report["rows"])
    for title, rows in report["sections"].items():
        lines += ["", f"-- {title}", *_grid(rows)]
    warn = []
    if t.get("unpriced"):
        warn.append(f"{t['unpriced']} requests UNPRICED, excluded from cost ({', '.join(t['unpriced_models'])})")
    if t.get("incomplete_output"):
        warn.append(f"{t['incomplete_output']} requests with incomplete output counts (cost is a lower bound; "
                    "see --level reconcile)")
    if t.get("assumed_price_models"):
        warn.append(f"assumed (unverified) prices: {', '.join(t['assumed_price_models'])}")
    if t.get("anomalies_ledger_wide"):
        warn.append("transcript anomalies (ledger-wide): "
                    + ", ".join(f"{k}={v}" for k, v in t["anomalies_ledger_wide"].items()))
    return "\n".join([*lines, *(["", *("! " + w for w in warn)] if warn else [])])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=LEVELS, default="session")
    ap.add_argument("--since", default=None, help="default 7d; 'all' for tree")
    ap.add_argument("--session")
    ap.add_argument("--project")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--format", choices=("table", "json"), default="table")
    ap.add_argument("--no-ingest", action="store_true")
    args = ap.parse_args(argv)
    args.since = args.since or ("all" if args.level == "tree" or args.session else "7d")
    con = ledger.connect()
    if not args.no_ingest:
        ingest.scan(con, ingest.transcripts(ledger.config_dir() / "projects"))
    report = run(con, args)
    print(json.dumps(report, indent=1) if args.format == "json" else table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
