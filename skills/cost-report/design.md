# cost-report: design

Usage, cost and limit tracking for Claude Code in a multi-agent setup. It measures tokens, API-list
cost, model and effort per session, per subagent and per skill, records them over time, and
(from phase 2) warns as a session nears its plan's limits so work winds down with a handoff
instead of stopping mid-edit.

## Status

**Stage: observing (between phase 1 and phase 2).** Phase 2 starts once the checks in §12 are met.

**Done and confirmed**

| Item | Where | Confirmed by |
|---|---|---|
| Phase 0 spikes S1–S4 | §3 | Pricing matched Claude Code to the cent; ccusage token totals matched |
| Phase 1 ledger, ingest, pricing, 11 report views, `/cost-report` | `SKILL.md` | 24 tests, ruff, mypy; real backfill; review-core run (1 fixed, 1 false positive) |
| Status line made diagnosable and shell-neutral | `scripts/statusline.py` | Bad input and BOM tests; live trace in `cost/statusline-last.json` |
| Hooks and status line enabled in `settings.json` | `SKILL.md` Setup | Status line rendered live; no `cost/errors.log` |
| Pro plan sends `rate_limits.five_hour` and `seven_day` | §3 (S2) | Live trace and snapshots |
| Transcript retention 365 days | `SKILL.md` Setup | `cleanupPeriodDays` in `settings.json` |

**Known issues, carried into phase 2**
- `limits` calibration (`usd_per_pct_median`) counts intervals the ledger hasn't ingested yet, so
  it can report $0 per 1%. Fix: use only intervals the ledger fully covers, and skip zero-cost
  ones. Nothing uses this figure yet.
- Not yet known: whether sessions in the VS Code panel run the status line. Only terminal
  sessions are confirmed. If they don't, snapshots exist only for terminal use (see §12).
- Subagent output is under-counted (§3), and background calls are missing. Both are reported,
  not hidden.

This file holds the decisions; `SKILL.md` holds operating instructions.

## 1. Goals and non-goals

**Goals**
- Per-request ledger with session → agent tree → request breakdown, model, effort, skill.
- Reports that answer named questions (§6), computed by scripts. The model only presents them.
- Both kinds of plan:
  - **subscription** (Pro/Max): limited by a rolling 5-hour window and a weekly window, in %;
  - **API / dollar or token budget**: limited by daily and/or weekly spend, in USD or tokens.
- Graceful wind-down near a limit (phase 3), not hard stops.
- History durable beyond Claude Code's transcript cleanup.

**Non-goals**
- Central/team service, pushing data anywhere, and Console reconciliation: single developer; on
  a subscription there is no invoice to reconcile.
- Storing prompt or response text. The ledger holds ids, token counts, metadata and task tags.

**Build vs buy.** `ccusage` parses the same transcripts. It is used once as an independent
cross-check (§3), not as the engine:
- it has no subagent tree, skill attribution, effort, or hooks;
- it would add a Node dependency to hooks;
- its dollar figures are wrong for 1-hour cache writes (§3).

## 2. Data sources

| Source | What it gives | Limits |
|---|---|---|
| Transcripts `<config>/projects/<proj>/<session>.jsonl` | One row per response content block: `requestId`, model, `effort`/`perTurnEffort`, full `usage` (5m/1h cache-write split, thinking, server tools, `speed`), `attributionSkill`, `version` | Internal format. Deleted after `cleanupPeriodDays` (default 30). Subagent output under-reported (§3). No background calls. |
| Subagent files `<session>/subagents/agent-<id>.jsonl` + `.meta.json` | Same rows; meta has `agentType`, `parentAgentId`, `spawnDepth`, `toolUseId`, requested model alias | Same. |
| Agent tool result in the parent (`toolUseResult`) | The subagent's **final** request usage, `resolvedModel`, `totalToolUseCount` | Final request only. |
| `cost-state` rows in transcripts | Claude Code's own per-session, per-model token and `costUSD` totals, including background calls | Written only by some sessions; a snapshot that can be stale if the session resumes. |
| Status line stdin JSON | `cost.total_cost_usd`; `rate_limits.five_hour` / `seven_day` (`used_percentage`, `resets_at`) for subscribers; `rate_limits.spend_limit` (`used_usd`, `limit_usd`) behind a gateway | Interactive sessions only; present only after the first response. |
| Hook stdin JSON | `session_id`, `transcript_path`, `prompt_id`; `agent_id`, `agent_type`, `agent_transcript_path` on SubagentStop; `stop_hook_active` on Stop/SubagentStop | `UserPromptSubmit` carries the prompt text: never stored. |
| OTel | `api_request` with `cost_usd`, effort, query source | Needs a resident receiver; deferred (§9). |

Transcripts are the ledger's primary source. Status line snapshots are the authoritative source
for subscription limits. `cost-state` and the parent's tool result are correction and
reconciliation sources.

## 3. Phase 0 findings (2026-10-03, Claude Code 2.1.286/2.1.287)

**S1. Pricing matches Claude Code exactly.** Three headless runs (`claude -p --output-format json`):

| Run | Claude Code `total_cost_usd` | Ledger | Gap |
|---|---|---|---|
| 1 | 0.118581 | 0.117580 | 940 input + 12 output tokens |
| 2 | 0.041555 | 0.040522 | 943 input + 18 output tokens |
| 3 | 0.028065 | 0.027065 | 940 input + 12 output tokens |

- Cache read and cache write tokens agree exactly.
- The gap is one background call per session that is not in the transcript.
- `total_cost_usd` is populated on the Pro plan (`costBasis: "list"`).
- Comparing `cost-state` with the ledger on sessions where both cover the same tokens also
  verifies the Opus 5.5 and Sonnet 5.5 rates to the cent.
- Background calls are not always small: one session had 55k Haiku input tokens (about $0.07)
  that never appear in its transcript.

**S2. The status line exposes the limits** (from the 2.1.287 binary's status line docs).
- Fields: `rate_limits.five_hour.used_percentage`, `seven_day.used_percentage`, each with
  `resets_at` in epoch seconds; `spend_limit` with optional `used_usd`/`limit_usd` behind a gateway.
- Still needs a live capture on the Pro plan; the phase-1 status line recorder provides it.
- Not yet verified: which shell runs the status line command.
- 2026-10-04: after the status line was enabled, three interactive terminal sessions ran but no
  snapshot or session cost was recorded. The cause is unknown: the command may not have run, may
  have received no input, or may have received no data. Response:
  - commands switched to shell-neutral absolute paths (no quotes, no `&`);
  - the script now decodes input with a BOM tolerated and writes UTF-8 output;
  - every call writes `cost/statusline-last.json` (key names only), which tells the three cases
    apart.

**First real-data finding (phase 1 smoke run, all history to 2026-10-03).**
- Cache reads are 97.6% of input-side tokens.
- 1-hour cache writes after idle gaps of over 60 minutes are about 15% of the API-equivalent
  total: context is rebuilt after a session sits idle.
- This is the first lever for phase 2 to show.

**S3. Hooks.**
- On Windows, hook commands can run in **PowerShell**: a quoted executable needs the call
  operator, `& "C:/path/python.exe" "script.py" args`.
- At `PostToolUse` the transcript already holds the requesting response with final usage, so a
  read of the new lines then is current.
- A background subagent's completion arrives as a `UserPromptSubmit` whose prompt is a
  `<task-notification>`.

**S4. Independent token check.**
- `ccusage` daily token totals equal the ledger's on every day, in all four token classes. The
  last day differed only because sessions were still being written.
- `ccusage` dollar totals are 4–14% lower on Opus 5.5 days. It does not apply the 2× rate to
  1-hour cache writes; Claude Code's own figures agree with the ledger, not with `ccusage`.

**Finding: subagent output is under-reported in transcripts.**
- 321 of 522 subagent requests never get a row with a `stop_reason`. Their only rows carry the
  usage snapshot from stream start (output 3–21 tokens).
- Main-thread requests are almost unaffected (2 of 1,212).
- Input and cache tokens are known at stream start, so they are correct; only output is low.
- Example: Sonnet subagents in one session show 270 output tokens; Claude Code counted 14,519.

Handling:
1. the parent's `toolUseResult.usage` restores each subagent's final request;
2. requests without final usage are flagged (`final_usage = 0`) and counted in every report;
3. sessions with a `cost-state` show Claude Code's figure next to the ledger's, with the
   difference reported as *unattributed* (background calls plus the output shortfall). It is never
   spread silently across rows.

## 4. Storage

- **Ledger location:** `$COST_LEDGER_DB`, else `$CLAUDE_CONFIG_DIR/cost/ledger.db` (already
  excluded by the repo's ignore-all `.gitignore`), else `~/.claude/cost/ledger.db`.
- **SQLite settings:** WAL mode and `busy_timeout`. Writes are idempotent upserts. Parallel
  sessions and hooks write concurrently.

Tables:

| Table | Key | Holds |
|---|---|---|
| `requests` | `request_id` | session, agent (NULL = main), ts, model, effort, speed, skill, token classes, web search count, `final_usage`, `cc_version`, `source` |
| `sessions` | `session_id` | project, cwd, branch, entrypoint, title, first/last ts; later task type and outcome |
| `agents` | `agent_id` | session, parent agent, type, description, depth, requested model, tool use id |
| `cc_reported` | `session_id, model` | latest `cost-state` totals and `costUSD` |
| `limit_snapshots` | `ts, window` | `used_pct`, `resets_at`, `used_usd`, `limit_usd`, session |
| `anomalies` | `path, kind, detail` | unrecognised rows, unpriced models, synthetic rows: counted, never priced at 0 silently |
| `ingest_state` | `path` | byte offset, size, mtime |

**Cost is computed at query time, never stored.**
- `prices.json` holds effective-dated rate entries per model. A registered SQLite function
  prices each row from its tokens using the entry in force at the row's timestamp.
- So a price change or correction needs no re-ingest, and old calls keep old rates.
- Model ids are normalised (date suffix and `[1m]` suffix stripped).
- Each entry records how it was verified:
  - `claude-code`: matched Claude Code's own cost;
  - `docs`: from the published price list;
  - `assumed`: standard multipliers.
- Fast mode multiplier and long-context rates are optional fields. Docs state that current
  1M-context models have no long-context premium.

**Incremental ingest.**
- Each file is read from its stored byte offset, and only whole lines are consumed, so a
  half-written last line is picked up next time.
- A file that shrank or was replaced is re-read from 0.
- Requests never span files (checked); a test guards it anyway.

**Retention.** `cleanupPeriodDays: 365` in `settings.json`. The `SessionStart` hook also runs an
incremental scan, so files are captured long before cleanup.

## 5. Components

```
skills/cost-report/
  SKILL.md            /cost-report: run a script, present its output, never recompute
  design.md           this file
  scripts/
    prices.json       effective-dated rates, with verification status
    ledger.py         paths, schema, connection, pricing (SQLite function)
    ingest.py         scan | file | hook   (incremental; hook mode fails open)
    rollup.py         report levels (§6), table or JSON
    statusline.py     status line: renders usage and records limit snapshots     (phase 1)
    budget.py         profiles, evaluator, warn hooks                            (phase 2)
    estimate.py       percentiles with outcomes                                  (phase 3)
  tests/
```

**Implementation rules**
- Scripts use only the standard library and run with the py312 interpreter by absolute path,
  never the bare `python` (a Store stub is on PATH).
- Hook and status line commands use PowerShell syntax (§3, S3).
- **Config validation:** budget profiles use strict dataclass validation; an unknown key raises.
  This departs from the global "pydantic" rule because hooks must stay dependency-free.

**Hook rules**
- Always exit 0 (exit 2 blocks the triggering action).
- Hard `timeout` in the hook config.
- Fail open, with errors logged to `<config>/cost/errors.log`.
- No network calls.
- Throttled where the hook fires per tool call.

## 6. Reports (questions they answer)

| Level / view | Question |
|---|---|
| `session` | Which sessions cost most; main-thread vs subagent split |
| `tree` | Within one session, which agent subtree spent what (self vs subtree) |
| `agent-type` | Which subagent types are expensive per run |
| `model-effort` | What each model × effort costs per request and per session |
| `day` | Spend over time; per local calendar day, split by each call's own timestamp |
| `skill` | What each skill (e.g. review-core) costs per use |
| `project` | Spend per repo |
| `cache` | Cache hit ratio; 5m vs 1h write volume and cost share; misses after idle gaps (cache is most of the token volume, and the main lever) |
| `fanout` | Subagent share of spend; cost by depth; cost per review run |
| `limits` | Recorded 5h/weekly % over time; API-equivalent $ per 1% of the weekly limit |
| `reconcile` | Ledger vs Claude Code's `cost-state` per session; unattributed gap; incomplete-output count |

Every report prints, in its header:
- total, main vs subagent split, cache read share;
- rows unpriced, unrecognised, or with incomplete output;
- models whose price is not verified.

## 7. Budget model (phase 2)

A **budget profile** (committed template, gitignored local copy) has a mode and windows:

```json
{
  "mode": "subscription",
  "windows": [
    {"name": "5h",     "kind": "subscription", "source": "five_hour"},
    {"name": "weekly", "kind": "subscription", "source": "seven_day"}
  ],
  "thresholds": {"notice": 50, "wrap_up": 80, "handoff": 95},
  "budget_note_at_start": false
}
```

```json
{
  "mode": "api",
  "windows": [
    {"name": "daily",  "kind": "calendar", "period": "day",  "limit": 20,  "unit": "usd"},
    {"name": "weekly", "kind": "calendar", "period": "week", "limit": 100, "unit": "usd", "week_starts": "mon"}
  ],
  "thresholds": {"notice": 50, "wrap_up": 80, "handoff": 95}
}
```

**Usage figure for each window**
- `subscription`: the latest status line snapshot, plus the ledger's API-equivalent spend since
  that snapshot converted at the calibrated $-per-% rate (`limits` view). Marked *authoritative*
  when fresh, *estimated* when extrapolated.
- `api`: ledger spend in the window, *measured*. A gateway `spend_limit` snapshot, if present,
  overrides it.
- **Subscription limits are shared** with claude.ai and other devices.
  - The calibrated $-per-% is therefore an upper bound.
  - The status line snapshot, not the ledger, is the truth for a subscription window.

**Evaluator**
- Each window yields used, limit, %, source and `resets_at`. The highest % binds.
- It is a pure function behind a small usage-source interface, tested without hooks or SQLite.

**Hooks**
- *Status line:* e.g. `5h 34% · wk 61% · session ≈$2.10`.
- *`PostToolUse` / `UserPromptSubmit`:* throttled. They read the new transcript lines, evaluate,
  and announce each threshold once per window via `additionalContext`.
- *SessionStart budget note:* off by default. When on, the session records that it was shown, so
  its effect on cost and outcomes can be measured.

## 8. Wind-down and planning (phase 3, redesigned after phase 1–2 data)

**Wind-down**
- At `wrap_up`: one instruction to finish the current step, checkpoint and write a handoff note.
- At `handoff`: a stronger stop-exploring message.
- No `PreToolUse` deny by default: a denied model keeps reasoning and retrying, each retry a
  full-context call. A hard gate is opt-in, `api` mode only, and still allows writing the handoff
  file.
- The `Stop` hook never blocks by default. When enabled, it fires once and honours
  `stop_hook_active`.

**Outcomes and planning**
- Outcome signals per session: commits, test runs and their results, tdd-spec disputes,
  review-core findings. Cost is judged **per completed task**.
- `/budget-plan`: p50/p75/p90 cost with completion rate per task type × model × effort, with
  sample size, and falls back to defaults (saying so) when history is thin.

## 9. Deferred

**OTel**
- It would close the background-call gap and give a live `cost_usd`.
- But it needs a resident receiver on Windows, and Claude Code's own figures already come from
  `-p` JSON, `cost-state` and the status line.
- Revisit if the unattributed gap in `reconcile` proves material.

## 10. Testing

- **Isolation:** synthetic transcripts in `tmp_path`; an autouse fixture points
  `COST_LEDGER_DB` and `CLAUDE_CONFIG_DIR` at temp paths.
- **Expected values:** computed by hand from the published rates, never by the code under test.
- **Fixtures:**
  - streamed duplicate rows;
  - a request with no final row;
  - parent handback correction;
  - a resumed session (two files, one session);
  - nested subagents (depth 2);
  - an unknown model;
  - the `<synthetic>` model;
  - a `[1m]` / dated model id;
  - a `requestId` in two files;
  - a session crossing local midnight;
  - a half-written last line;
  - a file that shrank;
  - concurrent writers;
  - a price change at an effective date;
  - fast mode.
- **Gate:** `ruff check`, `mypy`, `pytest` via py312 with default settings (as review-core).
- **Real-data checks:** ledger vs `ccusage` tokens, ledger vs Claude Code cost.

## 11. Phases

| Phase | Contents | Exit |
|---|---|---|
| 0 | Spikes S1–S4 | **Done** (§3) |
| 1 | Ledger, ingest (incremental, corrections, anomalies), query-time pricing, reports incl. cache/fan-out/reconcile/limits, `/cost-report`, status line recorder, `SessionStart`/`SessionEnd` hooks, retention setting | **Done**; live since 2026-10-05 |
| 1→2 | Observation: one full weekly limit cycle of real data | **In progress**; entry checks in §12 |
| 2 | Calibration fix, budget profiles (both modes), evaluator, warn-only hooks, status line binding-window display | Thresholds tuned on real data |
| 3 | Wind-down, outcome signals, `/budget-plan` | Redesigned after phase 2 |

## 12. Observation period (the gate from phase 1 to phase 2)

**Why wait.** Phase 2's warning thresholds and its $-per-% conversion have to come from real
data. Your usage pattern over a whole week, including how fast the 5-hour window fills and whether
the weekly limit binds first, can't be guessed.

**How long.**
- One full weekly cycle after snapshots start.
- Come back earlier if you hit a limit unexpectedly, or if the checks below show a problem.

**Meanwhile.**
- Use Claude Code as usual. Run some sessions in a terminal: the status line, which records the
  snapshots, is only confirmed there.
- Nothing else is required. `/cost-report limits` shows the data accumulating.

**Entry checks for phase 2** (all should hold):

| # | Check | How |
|---|---|---|
| 1 | One complete weekly window covered: `seven_day` snapshots from reset to reset, at least 10 | `/cost-report limits --since 2w` |
| 2 | At least 5 separate 5-hour windows, each with 3 or more snapshots | same, `recent snapshots` section with a higher `--limit` |
| 3 | No unexplained hook or status line errors | `cost/errors.log` absent or each entry explained |
| 4 | VS Code question answered: does `cost/statusline-last.json` update while you work only in the VS Code panel? | Check its `ts` after a panel-only session |
| 5 | Ledger quality is acceptable: unattributed gap and incomplete counts reviewed | `/cost-report reconcile --since 2w` |

**Questions the data must answer** (they set the phase 2 design):
- **Climb rate:** how fast does the 5-hour % rise per hour of active work, and which limit binds
  first in practice?
- **Calibration:** after the calibration fix, is $-per-1% stable for each window, so a
  subscription window can be extrapolated between snapshots?
- **Thresholds:** are 50/80/95% the right notice, wrap-up and handoff points for this usage?
- **Cache lever:** is the idle-gap cache rewrite share (about 15% at first measurement) still
  the largest lever, or is something else (`cache`, `fanout`)?

**Phase 2 build order on return:**
1. calibration fix;
2. budget profiles for both modes;
3. evaluator;
4. status line binding-window display;
5. warn-only hooks.
