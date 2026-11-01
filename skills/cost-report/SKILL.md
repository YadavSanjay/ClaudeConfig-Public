---
name: cost-report
description: >-
  Report Claude Code token usage, API-equivalent cost and plan-limit usage from a local ledger
  built from session transcripts: per session, per subagent (with the nested agent tree), per
  subagent type, model × effort, day, skill, project, cache behaviour, subagent fan-out, recorded
  5-hour/weekly limit snapshots, and reconciliation against Claude Code's own cost figure. Use
  when asked what something cost, how many tokens a session or subagent used, where spend goes,
  how close the plan limits are, which skills or agents are expensive, or about cache efficiency;
  or by name (/cost-report). Numbers come only from the scripts; never estimate them.
---

# cost-report: usage, cost and limits

Args: `[level] [window] [--session ID] [--project NAME]`. Defaults: `session`, last 7 days.
Design and decisions: [design.md](design.md).

## Run

Use one fixed Python 3.12 interpreter, written `<python>` below, and never a bare `python`: on
Windows it may be a Store stub. From PowerShell:

```powershell
& "<python>" "$env:CLAUDE_CONFIG_DIR/skills/cost-report/scripts/rollup.py" --level <level> --since <window> --format json
```

1. **Pick the level from the question.**

   | Question | Level |
   |---|---|
   | Most expensive sessions; main vs subagent split | `session` |
   | One session, broken down by agent | `tree --session <id prefix>` |
   | Which subagent types cost most | `agent-type` |
   | What each model × effort costs | `model-effort` |
   | Spend over time | `day` |
   | Cost per skill | `skill` |
   | Cost per repo | `project` |
   | Cache efficiency; idle-gap rewrites | `cache` |
   | How much fan-out costs | `fanout` |
   | How close the 5-hour/weekly limits are | `limits` |
   | Does the ledger match Claude Code | `reconcile` |

   Window: `--since 24h|7d|2w|YYYY-MM-DD|all`.
2. **Run the script.** It first ingests new transcript lines, then prints JSON.
3. **Present the result.**
   - Show a compact table of `rows` (and any `sections`).
   - Add two or three observations taken from the numbers, for example the top cost driver, the
     subagent share, or the cache-write cost after idle gaps.
4. **Always state the cost basis.** Figures are **API list prices**. On a Pro/Max subscription
   they are an API-equivalent comparison, not a bill. The plan's real limits are the 5-hour and
   weekly percentages in `limits`.
5. **Repeat every warning in `totals`**:
   - `incomplete_output` (cost is a lower bound);
   - `unpriced` / `unpriced_models`;
   - `assumed_price_models`;
   - `anomalies_ledger_wide`.

Do not compute, extrapolate or round figures beyond what the JSON contains. If the script fails,
report its error output as it is.

Other commands:

```powershell
$py = "<python>"; $s = "$env:CLAUDE_CONFIG_DIR/skills/cost-report/scripts"
& $py "$s/ingest.py" scan            # incremental ingest of every transcript
& $py "$s/ingest.py" scan --full     # re-read everything (idempotent)
& $py "$s/rollup.py" --level day --since 2w      # table output for a human
```

The ledger is `$CLAUDE_CONFIG_DIR/cost/ledger.db`; override it with `$COST_LEDGER_DB`. It's local
runtime data, ignored by git, and holds no prompt or response text.

## Setup (once per machine)

1. **Backfill:** `& $py "$s/ingest.py" scan`.
2. **Retention:** `settings.json` sets `"cleanupPeriodDays": 365`, so transcripts outlive the
   default 30 days.
3. **Hooks and status line.** Merge this into `settings.json`, with `<python>` and `<config>` as
   absolute paths ([settings.example.json](../../settings.example.json) has the same block).

   ```json
   "hooks": {
     "SessionStart": [{ "hooks": [{ "type": "command", "timeout": 60,
       "command": "<python> <config>/skills/cost-report/scripts/ingest.py hook --scan" }]}],
     "SessionEnd": [{ "hooks": [{ "type": "command", "timeout": 30,
       "command": "<python> <config>/skills/cost-report/scripts/ingest.py hook" }]}]
   },
   "statusLine": { "type": "command",
     "command": "<python> <config>/skills/cost-report/scripts/statusline.py" }
   ```

   - **Shell-neutral commands.** Absolute paths with no spaces need no quotes and no PowerShell
     `&`, so the same command works whether Claude Code runs it through PowerShell, Git Bash or
     `cmd`. Hooks run through PowerShell on this machine; the status line's shell is unconfirmed.
   - **The hooks** always exit 0 and print nothing; errors go to `cost/errors.log`.
   - **The status line** shows `[model] · 5h 34% (Sat 14:00) · wk 61% (Mon 09:00) · session $2.10 API-eq`.
     It records each change in a limit to the ledger, which feeds `--level limits`.
   - **The paths** are machine-specific, like the Node path in `CLAUDE.md`.

   **If `--level limits` stays empty,** read `cost/statusline-last.json`. The status line rewrites
   it on every refresh, with key names only (no values).
   - **The file is missing:** Claude Code never ran the command. Check the `statusLine` block and
     restart the session.
   - **`error` is `"empty stdin"`:** the command ran but got no input. The status line also shows
     `[cost-report: empty stdin]` in that case.
   - **`rate_limits` is `null` but `has_cost` is true:** Claude Code isn't sending plan limits for
     this account. Subscription limits then have to be estimated (design.md section 7).

## Known limits

- **Subagent output is under-counted.** Subagent transcripts often miss the final output count.
  Each subagent's last request is restored from the parent's tool result; the rest stay flagged
  as `incomplete`.
- **Background calls are missing.** Title generation and similar calls never appear in
  transcripts. `reconcile` shows them as the unattributed gap, where Claude Code wrote its own
  figure.
- **Subscription limits are shared.** They are also consumed by claude.ai and other devices, so
  `usd_per_pct` in `limits` is an upper-bound estimate.
