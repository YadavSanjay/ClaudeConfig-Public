"""cost-report status line: show plan limits and session cost, and record limit snapshots.
Design: ../design.md (sections 2 and 7).

Claude Code pipes a JSON object to the status line command on every refresh. This script reads:

    rate_limits.five_hour / seven_day   {used_percentage, resets_at}   subscribers (Pro/Max)
    rate_limits.spend_limit             {used_percentage, resets_at, used_usd?, limit_usd?}
                                                                       behind a gateway spend limit
    cost.total_cost_usd                 Claude Code's own session cost (API list price)

Each limit is recorded in limit_snapshots only when its value changes, which builds the history
the `limits` report and the phase-2 budget hooks use. Prints one line; never fails (a broken
ledger only drops the recording).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any

import ledger

LIMITS = (("five_hour", "5h"), ("seven_day", "wk"), ("spend_limit", "spend"))


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(data: dict[str, Any]) -> None:
    limits = data.get("rate_limits") or {}
    sid = data.get("session_id")
    total = (data.get("cost") or {}).get("total_cost_usd")
    if not limits and total is None:
        return
    con = ledger.connect()
    try:
        with con:
            for name, _ in LIMITS:
                w = limits.get(name)
                if not isinstance(w, dict) or w.get("used_percentage") is None:
                    continue
                row = (float(w["used_percentage"]), _iso(w.get("resets_at")), w.get("used_usd"), w.get("limit_usd"))
                last = con.execute(
                    "SELECT used_pct, resets_at, used_usd, limit_usd FROM limit_snapshots "
                    "WHERE limit_name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
                if last is None or tuple(last) != row:
                    con.execute("INSERT OR IGNORE INTO limit_snapshots VALUES (?,?,?,?,?,?,?)",
                                (_now(), name, *row, sid))
            if sid and isinstance(total, (int, float)):
                con.execute(
                    "INSERT INTO sessions (session_id, cc_total_usd, cc_total_ts) VALUES (?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET cc_total_usd=excluded.cc_total_usd, "
                    "cc_total_ts=excluded.cc_total_ts", (sid, float(total), _now()))
    finally:
        con.close()


def render(data: dict[str, Any]) -> str:
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(f"[{model}]")
    limits = data.get("rate_limits") or {}
    for name, label in LIMITS:
        w = limits.get(name)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            text = f"{label} {w['used_percentage']:.0f}%"
            if isinstance(w.get("resets_at"), (int, float)):
                text += dt.datetime.fromtimestamp(w["resets_at"]).strftime(" (%a %H:%M)")
            parts.append(text)
    total = (data.get("cost") or {}).get("total_cost_usd")
    if isinstance(total, (int, float)):
        parts.append(f"session ${total:.2f} API-eq")
    return " · ".join(parts)


def parse(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """The status JSON, tolerating a UTF-8 BOM (PowerShell 5.1 adds one when piping)."""
    if not raw.strip():
        return {}, "empty stdin"
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"bad json: {exc}"
    return (data, None) if isinstance(data, dict) else ({}, "not a JSON object")


def trace(raw: bytes, data: dict[str, Any], error: str | None) -> None:
    """Overwrite cost/statusline-last.json: proof the command ran and what shape it received.
    Key names only, no values, so it never holds paths or session content."""
    limits = data.get("rate_limits")
    info = {
        "ts": _now(), "stdin_bytes": len(raw), "error": error, "keys": sorted(data),
        "rate_limits": sorted(limits) if isinstance(limits, dict) else None,
        "has_cost": isinstance((data.get("cost") or {}).get("total_cost_usd"), (int, float)),
    }
    path = ledger.ledger_path().parent / "statusline-last.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, indent=1), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Claude Code reads UTF-8
    except (AttributeError, ValueError):
        pass
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""
    data, error = parse(raw)
    try:
        trace(raw, data, error)
        record(data)
    except Exception as exc:  # noqa: BLE001 - the status line must always print
        try:
            import ingest

            ingest.log_error(f"statusline: {exc!r}")
        except Exception:  # noqa: BLE001
            pass
    print(render(data) or (f"[cost-report: {error}]" if error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
