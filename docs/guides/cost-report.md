# cost-report: a guide for first-time readers

cost-report is a Claude Code skill that tells you how many tokens your work used, what it would
cost at API prices, and how close you are to your plan's limits. You run it as `/cost-report`.
This page explains why it exists, how it works, what to watch for, and what isn't built yet.

For the exact rules, see the skill's own files:
- [SKILL.md](../../skills/cost-report/SKILL.md) for the operating steps and setup;
- [design.md](../../skills/cost-report/design.md) for decisions, findings and the roadmap.

## Why we do this

Work here is spread across many sessions and subagents: a review alone can start three or four
agents. Without measurement you can't answer simple questions:

- Which sessions, skills or agent types cost the most?
- Is a `deep` review worth its cost compared with `standard`?
- How fast do I use up the 5-hour and weekly limits, and which one runs out first?
- How much is wasted when a session sits idle and its cache has to be rebuilt?

Claude Code shows a running cost per session, but it doesn't keep history, doesn't break cost
down by subagent or skill, and forgets everything when old transcripts are cleaned up.

## How it works, in plain terms

```
session transcripts ──► ingest (script) ──► ledger.db (SQLite) ──► rollup (script) ──► report
(Claude Code writes                          tokens + metadata,       prices applied
 these anyway)                                no prompt text           at query time
status line ──► limit snapshots (5-hour %, weekly %) ──┘
```

1. **Claude Code already records every request** in its session transcripts: model, effort,
   token counts, which skill was active, and which subagent made it.
2. **Ingest** reads only the new lines of each transcript and stores the numbers in a local
   SQLite database, the *ledger*. It never stores prompt or response text.
3. **Prices are applied when you ask, not when data is stored.** `prices.json` lists each
   model's rates with the date they took effect. A price correction needs no re-import, and old
   requests keep their old rates.
4. **The status line** shows your current 5-hour and weekly limit percentages. Each time a
   percentage changes, it records a snapshot to the ledger. That's the only source for real
   subscription limits.
5. **Rollup** answers one question per *level* and prints a table or JSON. The model only
   presents the numbers; it never calculates or estimates them itself.

Optional hooks run ingest at the start and end of every session, so the ledger stays current
without you thinking about it. Transcripts are kept for 365 days instead of the default 30.

### The questions it answers

| You want to know | Level |
|---|---|
| Which sessions cost most; main thread vs subagents | `session` |
| One session broken down by agent | `tree --session <id>` |
| Which subagent types are expensive | `agent-type` |
| Cost per model and effort setting | `model-effort` |
| Spend over time | `day` |
| Cost per skill, for example review-core | `skill` |
| Cost per repo | `project` |
| Cache efficiency; cost of idle gaps | `cache` |
| What fanning out to subagents costs | `fanout` |
| How close the plan limits are | `limits` |
| Does the ledger agree with Claude Code's own figure | `reconcile` |

Examples:

```
/cost-report                       # sessions, last 7 days
/cost-report skill 2w              # cost per skill over two weeks
/cost-report limits                # recorded 5-hour and weekly limit history
```

## Important: what the dollar figures mean

The figures are **API list prices**. On a Pro or Max subscription you don't pay them; they are
a like-for-like way to compare work. What actually limits a subscription is the 5-hour and
weekly **percentages**, shown under `limits`.

## When it works well

- **Comparing work:** which skill, model or effort level costs more, per run or per day. The
  token counts were checked against an independent tool (`ccusage`) and matched on every day.
- **Main-thread costs:** checked against Claude Code's own cost to the cent, apart from one
  small background call per session (see below).
- **Cache behaviour:** the first real-data run showed that rebuilding the cache after idle
  gaps of over an hour was about 15% of all cost. That's the kind of lever it's built to find.

## Edge cases to be aware of

Every report prints its own warnings. Read them; they say when a number is a lower bound.

- **Subagent output is under-counted.** Subagent transcripts often miss the final output count.
  The skill restores each subagent's last request from the parent's record and flags the rest
  as *incomplete*. Input and cache tokens are unaffected.
- **Background calls are missing.** Title generation and similar calls never appear in
  transcripts. `reconcile` shows the gap as *unattributed*; it's never spread silently over
  other rows.
- **Subscription limits are shared** with claude.ai and your other devices. The ledger only sees
  this machine, so "dollars per 1% of the weekly limit" is an upper-bound estimate.
- **Limit snapshots need the status line to run.** It's confirmed in terminal sessions. Whether
  it runs in the VS Code panel is still being checked. If `limits` stays empty, `SKILL.md`
  explains how to diagnose it from `cost/statusline-last.json`.
- **Unknown models** are reported as *unpriced*, never priced at zero. Prices marked *assumed*
  are flagged too.
- **The setup snippets need your paths.** Replace `<python>` and `<config>` with the Python
  interpreter and the config folder, as absolute paths.
- **The ledger is local.** `cost/ledger.db` is never committed, so each machine has its own
  history.

## Not implemented yet

The skill is in an **observation period**: it's collecting a full weekly cycle of real limit
data (until about mid-October 2026) before the next phase is designed on top of it. See
[design.md §12](../../skills/cost-report/design.md).

- **Limit warnings (phase 2):** notices at about 50%, 80% and 95% of a limit, for both
  subscription plans (percentages) and API budgets (dollars per day or week). Thresholds will be
  tuned on the observed data.
- **A calibration fix:** the "dollars per 1%" figure can currently read $0 because it counts
  periods the ledger hasn't fully ingested. Nothing depends on it yet.
- **Graceful wind-down (phase 3):** near a limit, ask the agent to finish its current step and
  write a handoff note, instead of stopping mid-edit. Also cost per completed task, and a
  `/budget-plan` that predicts what a task type usually costs.
- **Closing the background-call gap** would need OpenTelemetry and a resident receiver. Deferred
  unless the gap turns out to matter.

## Words used on this page

| Term | Meaning |
|---|---|
| Ledger | The local SQLite database of requests, sessions, agents and limit snapshots. |
| API-equivalent cost | What the tokens would cost at published API prices. Not a bill on a subscription. |
| Cache read / cache write | Reusing context already sent (cheap) vs sending it again to be stored (more expensive, especially for the 1-hour cache). |
| Limit snapshot | A recorded 5-hour or weekly percentage, taken from the status line. |
| Reconcile | Comparing the ledger with Claude Code's own cost figure for the same session. |
| Fan-out | Starting subagents. Each one has its own context and cost. |
