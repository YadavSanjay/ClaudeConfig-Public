# ai-harness-engineering: a guide for first-time readers

ai-harness-engineering is a Claude Code skill that **audits** a project's AI-agent setup against a
written standard, scores it, and proposes a prioritised plan to fix the gaps. It doesn't change
anything until you agree to the plan. This page explains why it exists, how it works, what to
watch for, and what it doesn't do.

For the exact rules, see the skill's own files:
- [SKILL.md](../../skills/ai-harness-engineering/SKILL.md) for the workflow and the output it
  must produce;
- [references/standard-2026-07.md](../../skills/ai-harness-engineering/references/standard-2026-07.md),
  the dated standard with a checklist per dimension;
- [references/refactor-playbook.md](../../skills/ai-harness-engineering/references/refactor-playbook.md)
  for how to sequence the fixes.

## Why we do this

The **harness** is everything around the model that turns it into an agent that ships work:
the instructions it loads (`CLAUDE.md`), its skills and subagents, the hooks that check its
work, how it remembers progress, how cost is tracked, and what stops it when something goes
wrong.

As models get better, more of the quality comes from the harness rather than from the prompt.
But each project tends to grow its harness piece by piece, so:

- some projects have strong safety checks and no cost tracking, others the reverse;
- the agent sometimes grades its own work, which hides mistakes;
- rules that should be enforced by a script are written as polite requests in a prompt;
- pieces added for an older, weaker model stay around and cost tokens every session.

This skill gives every project the **same yardstick** and a repeatable way to close the gaps.
Several conventions in the global `CLAUDE.md` come from it: test fakes behind a `base.py`
interface, auto-format hooks, a Stop gate on a red build, and per-call cost tracking.

## How it works, in plain terms

The skill is a set of instructions and two reference documents. It has no scripts of its own:
the agent running it does the reading and scoring.

```
1. Inventory      2. Score            3. Gap report          4. Refactor plan
─────────────     ─────────────       ─────────────          ─────────────────
find what the     each of 9           table: dimension →     ordered changes, each
project has:      dimensions:         score → biggest gap    with an acceptance check
CLAUDE.md,        Absent, Partial     → impact               ──► you agree ──► only
hooks, skills,    or Solid, with a                                then any edits
agents, cost      file as evidence
code, evals ...
```

### The nine dimensions

| # | Dimension | What "good" looks like |
|---|---|---|
| 1 | Context and memory | Progress and plans live in files, not only in the conversation. `CLAUDE.md` stays short. |
| 2 | Planning | A spec exists before code, with a checkable list of what's in and out of scope. |
| 3 | Execution | Work is split across subagents that return summaries; parallel agents use separate worktrees. |
| 4 | Evaluation | **The doer isn't the judge.** Something other than the agent that wrote the code decides it's done: a script gate, or a fresh reviewer that assumes failure until shown otherwise. |
| 5 | Guardrails (hooks) | Rules that matter are enforced by hooks: guard protected files and secrets, format on edit, block stopping on a failing build. |
| 6 | Prompt and tool hygiene | Prompts are versioned files, external services sit behind interfaces with test fakes, structured output is validated, and each agent gets only the tools it needs. |
| 7 | Cost tracking and controls | Every run's cost is recorded, loops have token or turn caps, a cheap model is the default, and repeated failures trip a circuit breaker. |
| 8 | Autonomy safety and recovery | You can roll back, runaway processes are caught, runs are traced, and unattended runs follow an explicit policy. |
| 9 | Evolution | After a major model release, harness pieces are switched off one at a time, and only the ones still needed are kept. |

### How fixes are prioritised

| Priority | What | Why first |
|---|---|---|
| P0 | Safety and cost: caps, circuit breaker, cost telemetry, guards for secrets, a working rollback | An unbounded loop can burn the budget or damage files tonight. |
| P1 | Evaluation: a script gate and a fresh, read-only reviewer | The biggest gain in quality. |
| P2 | Structure and hygiene: prompt registry, fakes, slimmer `CLAUDE.md`, model tiering | Valuable, but nothing breaks while it waits. |

The playbook also fixes the **order** of changes: measure cost before you add caps, add gates
before you automate loops, write the fake in the same change as the interface, and change one
dimension per change set.

## How to use it

Ask in plain words, for example:

- "Audit this project's agent harness."
- "Are we following current best practice for Claude Code here?"
- "Add cost tracking and cost controls to this project."

You get, in this order:
1. a one-paragraph maturity summary;
2. the scored table of the nine dimensions;
3. the prioritised plan, each item with the check that proves it's done.

The agent then asks whether to start on P0 or hand the plan back. You can ask it to save the
report in the project, for example `docs/harness-audit-<date>.md`.

## When it works well

- **Setting up a new project:** it gives a checklist of what to put in place from day one.
- **Comparing projects:** the same nine dimensions make it easy to see which repo lags where.
- **Before automating:** before turning on scheduled or unattended runs, it checks that gates,
  caps and rollback exist first.
- **After a model upgrade:** dimension 9 is the prompt to remove scaffolding that is no longer
  needed.

## Edge cases to be aware of

- **The standard is dated (July 2026).** Claude Code's features change quickly. Treat the
  "verified primitives" table as true when written, and check that a feature still exists
  before relying on it. When the standard is revised, it's copied to a new dated file, never
  overwritten.
- **Scores are judgement, not measurement.** There's no script behind them. Each score must cite
  a file as evidence; challenge a score that doesn't.
- **"Absent" isn't always wrong.** A small or short-lived project may not need a prompt registry
  or model tiering. Decide per project whether a gap matters.
- **Hooks are OS-specific.** On this Windows machine hook commands run through PowerShell, and
  there's no `/proc` for spotting runaway processes. The playbook has notes for both Windows and
  POSIX; the plan must say which it targets.
- **Not every best practice suits every repo.** A project's own `CLAUDE.md` overrides the global
  conventions, and the same applies to the audit's recommendations.
- **It never edits before you agree.** Harness changes touch safety and money, so the plan comes
  first.

## Not implemented yet

- **No automated scoring.** There's no script that checks a repo against the standard; each audit
  is done by reading.
- **No record of past audits.** Unlike review-core and tdd-spec, there's no history or status
  file, so progress between audits is only visible if you saved the reports.
- **No scheduled re-audit.** The standard asks for a re-audit after major model releases, but
  nothing reminds you.
- **The standard hasn't been revised since its first version** (rev 2026-07). Parts of it are
  now covered by the other skills here: tdd-spec for planning and evaluation, review-core for a
  fresh-context reviewer, cost-report for cost tracking.

## Words used on this page

| Term | Meaning |
|---|---|
| Harness | Everything around the model: instructions, tools, hooks, subagents, memory, checks and cost controls. |
| Doer ≠ judge | The agent that does the work isn't the one that decides it's finished. |
| Stop gate | A hook that runs when the agent tries to finish, and blocks it if, for example, the build is red. |
| Circuit breaker | Stops a run after several failures in a row, instead of retrying forever. |
| Model tiering | Using a cheaper model by default and escalating to a stronger one only when quality needs it. |
| Test fake | A stand-in for an external service (network, LLM, storage) so tests run offline. |
