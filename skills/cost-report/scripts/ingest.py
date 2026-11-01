"""cost-report ingest: load Claude Code session transcripts into the ledger. Design: ../design.md.

    ingest.py scan [--root DIR] [--full]   every transcript under <config>/projects (incremental)
    ingest.py file PATH...                 specific transcripts (re-read from the start)
    ingest.py hook [--scan]                hook mode: ingest the transcript(s) named in the hook's
                                           stdin JSON (--scan: every changed transcript). Always
                                           exits 0 and prints nothing; errors go to cost/errors.log

Transcript layout (Claude Code 2.1.x; an internal format, so parsing is defensive):

    <root>/<project>/<session>.jsonl                             main thread
    <root>/<project>/<session>/subagents/agent-<id>.jsonl        one subagent run
    <root>/<project>/<session>/subagents/agent-<id>.meta.json    agentType, parentAgentId, ...

One API call is written as one row per content block, all sharing a requestId. Usage on each row
is a snapshot: input and cache counts are final from the start, output grows while the response
streams, so every token column upserts with MAX(). A request with no row carrying a stop_reason
never got its final output count (common for subagents, design.md section 3); its subagent's
final request is restored from the parent's Agent tool result. Files are read from a stored
byte offset, whole lines only, so ingest is incremental and idempotent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import ledger

SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Batch:
    """Everything one chunk of one transcript contributes to the ledger."""

    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    cc_reported: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    anomalies: dict[str, int] = field(default_factory=dict)

    def anomaly(self, kind: str) -> None:
        self.anomalies[kind] = self.anomalies.get(kind, 0) + 1


# =========================================================================== parsing
def usage_tokens(u: dict[str, Any]) -> dict[str, int]:
    cc = u.get("cache_creation") or {}
    w5, w1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
    if not cc:  # no TTL split in the payload: price as the 5-minute default
        w5 = int(u.get("cache_creation_input_tokens") or 0)
    stu = u.get("server_tool_use") or {}
    return {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "thinking_tokens": int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_write_5m": w5,
        "cache_write_1h": w1,
        "web_search": int(stu.get("web_search_requests") or 0),
        "web_fetch": int(stu.get("web_fetch_requests") or 0),
    }


def agent_id_from_path(path: Path) -> str | None:
    if path.parent.name == "subagents" and path.stem.startswith("agent-"):
        return path.stem[len("agent-"):]
    return None


def _session(batch: Batch, sid: str) -> dict[str, Any]:
    return batch.sessions.setdefault(sid, {"session_id": sid})


def parse_rows(rows: Iterator[dict[str, Any] | None], path: Path) -> Batch:
    """Fold transcript rows (None = an unparseable line) into a Batch."""
    batch = Batch()
    file_agent = agent_id_from_path(path)
    for row in rows:
        if row is None:
            batch.anomaly("bad_json")
            continue
        kind, sid = row.get("type"), row.get("sessionId")
        if not sid:
            continue
        info = _session(batch, sid)
        if kind == "ai-title" and row.get("aiTitle"):
            info["title"] = row["aiTitle"]
        elif kind == "cost-state":
            _cost_state(batch, sid, row)
        ts = row.get("timestamp")
        if ts:
            info["first_ts"] = min(info.get("first_ts") or ts, ts)
            info["last_ts"] = max(info.get("last_ts") or ts, ts)
        if file_agent is None:  # a subagent's cwd describes the agent, not the session
            for col, key in (("cwd", "cwd"), ("git_branch", "gitBranch"), ("entrypoint", "entrypoint")):
                if row.get(key):
                    info[col] = row[key]
        if kind == "user":
            _handback(batch, sid, row)
        elif kind == "assistant" and not row.get("isApiErrorMessage"):
            _request(batch, sid, row, file_agent)
    return batch


def _request(batch: Batch, sid: str, row: dict[str, Any], file_agent: str | None) -> None:
    msg = row.get("message")
    if not isinstance(msg, dict):
        msg = {}
    if msg.get("model") == SYNTHETIC_MODEL:
        batch.anomaly("synthetic")
        return
    rid, usage, ts = row.get("requestId"), msg.get("usage"), row.get("timestamp")
    if not rid or not isinstance(usage, dict) or not ts:
        batch.anomaly("no_usage")
        return
    tokens = usage_tokens(usage)
    final = int(msg.get("stop_reason") is not None)
    prev = batch.requests.get(rid)
    if prev is not None:
        for col, val in tokens.items():
            prev[col] = max(prev[col], val)
        prev["final_usage"] = max(prev["final_usage"], final)
        prev["ts"] = min(prev["ts"], ts)
        return
    batch.requests[rid] = {
        "request_id": rid, "ts": ts, "session_id": sid,
        "agent_id": row.get("agentId") or file_agent,
        "model": msg.get("model"), "effort": row.get("perTurnEffort") or row.get("effort"),
        "speed": usage.get("speed"), "skill": row.get("attributionSkill"),
        "final_usage": final, "cc_version": row.get("version"), "source": "transcript", **tokens,
    }


def _handback(batch: Batch, sid: str, row: dict[str, Any]) -> None:
    """The parent's Agent tool result: the subagent's final request usage and its tool count."""
    res = row.get("toolUseResult")
    if not isinstance(res, dict) or not res.get("agentId") or not isinstance(res.get("usage"), dict):
        return
    tokens = usage_tokens(res["usage"])
    batch.agents[res["agentId"]] = {
        "agent_id": res["agentId"], "session_id": sid, "agent_type": res.get("agentType"),
        "resolved_model": res.get("resolvedModel"), "tool_uses": res.get("totalToolUseCount"),
        "final_input": tokens["input_tokens"], "final_cache_read": tokens["cache_read"],
        "final_output": tokens["output_tokens"], "final_thinking": tokens["thinking_tokens"],
    }


def _cost_state(batch: Batch, sid: str, row: dict[str, Any]) -> None:
    """Claude Code's running totals for the session; the last one in the file wins."""
    usage = row.get("modelUsage")
    if not isinstance(usage, dict) or not row.get("totalCostUSD"):
        return
    batch.cc_reported[sid] = {
        model: {
            "input_tokens": u.get("inputTokens"), "output_tokens": u.get("outputTokens"),
            "cache_read": u.get("cacheReadInputTokens"), "cache_write": u.get("cacheCreationInputTokens"),
            "cost_usd": u.get("costUSD"),
        }
        for model, u in usage.items() if isinstance(u, dict)
    }


def agent_meta(path: Path) -> dict[str, Any] | None:
    """The agents row for a subagent transcript, from its .meta.json sidecar."""
    agent_id = agent_id_from_path(path)
    if agent_id is None:
        return None
    meta_file = path.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    return {
        "agent_id": agent_id, "session_id": path.parent.parent.name,
        "parent_agent_id": meta.get("parentAgentId"), "agent_type": meta.get("agentType"),
        "description": meta.get("description"), "spawn_depth": meta.get("spawnDepth"),
        "requested_model": meta.get("model"), "tool_use_id": meta.get("toolUseId"),
    }


# =========================================================================== reading
def read_new_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Whole lines after `offset`, and the offset just past the last complete line."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[: end + 1].splitlines(), offset + end + 1


def _decode(lines: list[bytes]) -> Iterator[dict[str, Any] | None]:
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            yield None
            continue
        yield row if isinstance(row, dict) else None


# =========================================================================== writing
def _project(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return Path(cwd.replace("\\", "/")).name or cwd


REQUEST_COLS = ("request_id", "ts", "session_id", "agent_id", "model", "effort", "speed", "skill",
                *ledger.TOKEN_COLS, "final_usage", "cc_version", "source")
UPSERT_REQUEST = (
    f"INSERT INTO requests ({', '.join(REQUEST_COLS)}) VALUES ({', '.join('?' * len(REQUEST_COLS))}) "
    "ON CONFLICT(request_id) DO UPDATE SET ts=MIN(ts, excluded.ts), "
    + ", ".join(f"{c}=MAX({c}, excluded.{c})" for c in (*ledger.TOKEN_COLS, "final_usage"))
)
AGENT_COLS = ("agent_id", "session_id", "parent_agent_id", "agent_type", "description", "spawn_depth",
              "requested_model", "resolved_model", "tool_use_id", "tool_uses", "final_input",
              "final_cache_read", "final_output", "final_thinking")


def write(con: sqlite3.Connection, batch: Batch, path_key: str) -> None:
    for r in batch.requests.values():
        con.execute(UPSERT_REQUEST, tuple(r[c] for c in REQUEST_COLS))
    for s in batch.sessions.values():
        con.execute(
            """INSERT INTO sessions (session_id, project, cwd, git_branch, entrypoint, title, first_ts, last_ts)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 project=COALESCE(excluded.project, project), cwd=COALESCE(excluded.cwd, cwd),
                 git_branch=COALESCE(excluded.git_branch, git_branch),
                 entrypoint=COALESCE(excluded.entrypoint, entrypoint), title=COALESCE(excluded.title, title),
                 first_ts=MIN(COALESCE(first_ts, excluded.first_ts), COALESCE(excluded.first_ts, first_ts)),
                 last_ts=MAX(COALESCE(last_ts, excluded.last_ts), COALESCE(excluded.last_ts, last_ts))""",
            (s["session_id"], _project(s.get("cwd")), s.get("cwd"), s.get("git_branch"), s.get("entrypoint"),
             s.get("title"), s.get("first_ts"), s.get("last_ts")),
        )
    for a in batch.agents.values():
        cols = [c for c in AGENT_COLS if a.get(c) is not None]
        con.execute(
            f"INSERT INTO agents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(agent_id) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "agent_id"),
            tuple(a[c] for c in cols),
        )
    for sid, models in batch.cc_reported.items():
        con.execute("DELETE FROM cc_reported WHERE session_id=?", (sid,))
        for model, u in models.items():
            con.execute(
                "INSERT INTO cc_reported VALUES (?,?,?,?,?,?,?)",
                (sid, model, u["input_tokens"], u["output_tokens"], u["cache_read"], u["cache_write"], u["cost_usd"]),
            )
    for kind, n in batch.anomalies.items():
        con.execute(
            "INSERT INTO anomalies (path, kind, count) VALUES (?,?,?) "
            "ON CONFLICT(path, kind) DO UPDATE SET count=count+excluded.count",
            (path_key, kind, n),
        )


def apply_handbacks(con: sqlite3.Connection) -> int:
    """Restore each subagent's final request output from its parent's Agent tool result."""
    cur = con.execute(
        """UPDATE requests SET
             output_tokens = (SELECT a.final_output FROM agents a WHERE a.agent_id = requests.agent_id),
             thinking_tokens = MAX(thinking_tokens,
                 (SELECT COALESCE(a.final_thinking, 0) FROM agents a WHERE a.agent_id = requests.agent_id)),
             final_usage = 1, source = 'handback'
           WHERE final_usage = 0 AND agent_id IS NOT NULL AND EXISTS (
             SELECT 1 FROM agents a WHERE a.agent_id = requests.agent_id
               AND a.final_input = requests.input_tokens AND a.final_cache_read = requests.cache_read
               AND a.final_output > requests.output_tokens)"""
    )
    return cur.rowcount


def ingest_file(con: sqlite3.Connection, path: Path, force: bool = False) -> int | None:
    """Ingest the new part of one transcript. None when nothing changed since the last ingest."""
    st = path.stat()
    key = str(path.resolve())
    seen = con.execute("SELECT offset, size, mtime FROM ingest_state WHERE path=?", (key,)).fetchone()
    offset = 0 if force or seen is None or st.st_size < seen["offset"] else seen["offset"]
    if seen is not None and not force and seen["size"] == st.st_size and seen["mtime"] == st.st_mtime:
        return None
    lines, new_offset = read_new_lines(path, offset)
    batch = parse_rows(_decode(lines), path)
    if (meta := agent_meta(path)) is not None:
        batch.agents[meta["agent_id"]] = {**meta, **batch.agents.get(meta["agent_id"], {})}
    with con:
        if offset == 0:
            con.execute("DELETE FROM anomalies WHERE path=?", (key,))
        write(con, batch, key)
        con.execute("INSERT OR REPLACE INTO ingest_state (path, offset, size, mtime) VALUES (?,?,?,?)",
                    (key, new_offset, st.st_size, st.st_mtime))
    return len(batch.requests)


def transcripts(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.jsonl") if "memory" not in p.parts)


def session_files(transcript: Path) -> list[Path]:
    """A main transcript plus every subagent transcript of the same session."""
    sub = transcript.with_suffix("") / "subagents"
    return [transcript, *sorted(sub.glob("agent-*.jsonl"))] if sub.is_dir() else [transcript]


def scan(con: sqlite3.Connection, paths: list[Path], force: bool = False) -> dict[str, int]:
    stats = {"files": 0, "unchanged": 0, "requests": 0, "errors": 0}
    for p in paths:
        try:
            n = ingest_file(con, p, force)
        except (OSError, sqlite3.Error) as exc:
            log_error(f"ingest {p}: {exc}")
            stats["errors"] += 1
            continue
        if n is None:
            stats["unchanged"] += 1
        else:
            stats["files"] += 1
            stats["requests"] += n
    with con:
        stats["handbacks_applied"] = apply_handbacks(con)
    return stats


def log_error(msg: str) -> None:
    """Append to <config>/cost/errors.log; hooks must never write to stdout or fail."""
    try:
        log = ledger.ledger_path().parent / "errors.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass
    print(msg, file=sys.stderr)


def hook(full_scan: bool) -> int:
    """SessionStart / SessionEnd / SubagentStop hook. Fails open: always returns 0."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        paths: list[Path] = []
        if full_scan:
            paths = transcripts(ledger.config_dir() / "projects")
        else:
            for key in ("transcript_path", "agent_transcript_path"):
                if (tp := payload.get(key)) and Path(tp).is_file():
                    paths += session_files(Path(tp)) if key == "transcript_path" else [Path(tp)]
        if paths:
            con = ledger.connect()
            try:
                scan(con, list(dict.fromkeys(paths)))
            finally:
                con.close()
    except Exception:  # noqa: BLE001 - a broken ledger must never break a session
        log_error("hook: " + traceback.format_exc(limit=3))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--root", type=Path, default=None)
    s.add_argument("--full", action="store_true", help="re-read every file from the start")
    f = sub.add_parser("file")
    f.add_argument("paths", nargs="+", type=Path)
    h = sub.add_parser("hook")
    h.add_argument("--scan", action="store_true", help="scan every changed transcript")
    args = ap.parse_args(argv)

    if args.cmd == "hook":
        return hook(args.scan)
    con = ledger.connect()
    if args.cmd == "scan":
        stats = scan(con, transcripts(args.root or ledger.config_dir() / "projects"), args.full)
    else:
        stats = scan(con, args.paths, force=True)
    print(json.dumps({"ledger": str(ledger.ledger_path()), **stats}))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
