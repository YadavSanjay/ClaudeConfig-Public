---
name: ai-harness-engineering
description: >-
  Audit and refactor a codebase's AI-agent harness (its Claude Code / Agent-SDK
  setup) to current state-of-the-art practice — verification loops and doer≠judge
  gates, deterministic hook and CI guards, environment-first containment,
  state-outside-context, subagent and workflow orchestration, prompt & tool
  hygiene, cost tracking + hard caps, and pruning scaffolding the model has
  outgrown. Use when asked to review, upgrade, standardize, harden, "bring in line
  with best practices", re-audit, or add cost governance to a project's harness,
  agent scaffolding, CLAUDE.md / AGENTS.md / rules / skills / hooks / subagents,
  or when setting these up for a new project. Produces a scored gap report +
  prioritized refactor plan. DATED standard (rev 2026-10) — re-audit after major
  model/tooling releases.
---

# AI Harness Engineering — audit & refactor

**What "the harness" is:** everything around the model weights that turns a
token predictor into an agent that ships work — the loop, tools, context
assembly, permission and containment layer, memory, verification, and multi-call
orchestration. This skill is the standard to measure a project against, plus the
procedure to close the gap.

> **This is a moving target.** The current standard lives in
> [references/standard-2026-10.md](references/standard-2026-10.md) and is dated.
> Every component encodes an assumption about what the model can't do, so prune
> what a stronger model has made unnecessary (Dimension 10). When you revise the
> standard, copy it to `standard-<new-date>.md`, edit the copy, and update the
> changelog at the bottom of this file. Never overwrite a dated standard.

## When to use

- "Review / upgrade / standardize / re-audit this project's agent harness."
- "Are we following current best practices for agents / Claude Code?"
- "Add cost tracking and cost controls to this project."
- "Set up hooks / subagents / evaluation / CLAUDE.md properly."
- Bootstrapping harness scaffolding for a brand-new project.

Not for: writing a single prompt, one-off tool calls, or non-agentic code.

## The through-line

1. **Build reactively, then prune.** Add a component for an observed failure,
   record why, give it a retire-when condition.
2. **Enforce with executable controls, not prose.**
3. **Contain at the environment layer first**, then steer the model.
4. **Measure the harness**, not only the product.

## The ten dimensions (what "good" is)

Full checklists are in the standard; this is the map.

1. **Context & memory** — the filesystem is memory; a short instruction file
   (`CLAUDE.md` / `AGENTS.md`, under ~200 lines, a table of contents), path-scoped
   rules and on-demand skills; plan/progress files; `/clear` + handoff over long
   corrected sessions; agent-written memory reviewed.
2. **Planning** — machine-checkable feature list and scope doc; interview → spec →
   fresh session; specs end in an end-to-end verification; plan mode only when the
   diff isn't one sentence.
3. **Execution** — summary-returning subagents with minimal tools; dynamic workflows
   for large fan-out; worktrees for parallel edits; parallelism for independent work,
   not role-play pipelines.
4. **Evaluation — doer ≠ judge** — a runnable check and shown evidence; the gate
   ladder (prompt check → `/goal` → Stop hook → calibrated second-opinion reviewer
   only where warranted); product evals with deterministic graders, measured noise,
   and transcripts read.
5. **Deterministic guardrails** — PreToolUse guards (fail closed), PostToolUse lint,
   Stop gate; the same guards in CI; every guard tested both ways; no prose-only rules.
6. **Containment & permissions** — sandbox / devcontainer / VM first; egress denied
   by default; credentials outside the sandbox; narrow allowlists; classifier and
   approval prompts as layers only; untrusted input treated as such.
7. **Prompt & tool hygiene** — versioned prompt registry; interfaces with test fakes;
   schema-validated output; minimal tool surface; untrusted text passed as data.
8. **Cost** — hard caps vs advisory limits labelled; priced per-call telemetry and
   attribution; tiering or a cheap-executor + strong-advisor pattern; circuit breaker;
   harness cost justified against benefit.
9. **Autonomy safety & recovery** — git as rollback (checkpoints miss Bash);
   watchdog; observability; a written unattended-run policy.
10. **Harness evolution & measurement** — retire-when per component; remove one at a
    time after model releases; measure hook blocks and skill use; evaluate components
    against a baseline; dated standard and re-audit cadence.

## Audit workflow

Run these four phases. Keep the output as a written report (offer to save it to the
target repo, e.g. `docs/harness-audit-<date>.md`, or the repo's re-audit log).

**Phase 1 — Inventory.** Discover what exists: instruction files (`CLAUDE.md`,
`AGENTS.md`, `.claude/rules/`, module-level files, auto memory), `.claude/`
(skills, agents, hooks and permissions in settings, workflows), prompt registry,
LLM client / gateway, pricing and usage accounting, eval harness, tracing, scope
and plan files, CI checks, sandbox / container setup, and how dependencies are
abstracted. Note the stack and OS (hooks, watchdogs and the sandbox are
platform-specific). For a re-audit, also read the previous audit's register of
assumptions and gather evidence for each (usage data, escalation rates, eval runs).

**Phase 2 — Score.** For each of the ten dimensions, assign `Absent / Partial /
Solid` with one line of evidence (a path) and the most valuable missing piece. Use
the checklists in [references/standard-2026-10.md](references/standard-2026-10.md).
Flag components that look stale (Dimension 10) as removal candidates, not only gaps.

**Phase 3 — Gap report.** A table: dimension → score → top gap → impact. Call out
anything that risks *cost blowout*, *unsafe autonomy* or *credential exposure* first;
those are P0 regardless of how incomplete a structural dimension looks.

**Phase 4 — Refactor plan.** A prioritized, sequenced list of changes (add, modify
or **remove**), following [references/refactor-playbook.md](references/refactor-playbook.md):
**P0 containment, safety & cost → P1 verification & evaluation → P2 structure,
hygiene & pruning**. Each item: what, where, and the acceptance check that proves it.
Do **not** start editing until the plan is agreed.

## Output contract

Every run of this skill produces, in order:
1. A one-paragraph maturity summary ("Solid on 7/10; P0 gap is no hard budget on
   headless runs; two components are removal candidates").
2. The scored dimension table.
3. The prioritized plan with acceptance checks.
Then ask whether to execute P0 now or hand back the plan.

## Reference index

- [references/standard-2026-10.md](references/standard-2026-10.md) — the current
  dated standard: through-line, per-dimension checklists, the verified Claude Code
  primitive cheat-sheet, anti-patterns, sources.
- [references/standard-2026-07.md](references/standard-2026-07.md) — the previous
  revision, kept for diffing.
- [references/refactor-playbook.md](references/refactor-playbook.md) — how to bring
  a project in line: priorities, sequencing, platform notes, acceptance checks, and a
  worked example on an LLM/RAG Python repo.

## Changelog

- **2026-10-17** — rev 2026-10. Re-reviewed against Anthropic's 2026 engineering
  posts (harness design, Managed Agents, auto mode, containment, evals, infra noise),
  current Claude Code docs, OpenAI's harness-engineering write-up and the 2026
  marmelab survey. Added the through-line (build reactively and prune; executable
  controls; environment-first containment; measure the harness). New Dimension 6
  *Containment & permissions*; Dimension 9 renamed *Harness evolution & measurement*
  and renumbered 10. Evaluation reframed as a gate ladder with a conditional,
  calibrated reviewer and product-eval practice. Dropped the mandatory compaction
  policy and default initializer/sprint contracts; CLAUDE.md under ~200 lines,
  AGENTS.md, path-scoped rules. Cost: hard vs advisory caps, `--max-budget-usd`,
  advisor tool. Rollback: git, since checkpoints miss Bash. Refreshed primitive
  cheat-sheet (33 hook events, dynamic workflows, auto mode, sandbox, plugin evals).
- **2026-07-13** — rev 1. Initial standard distilled from Anthropic long-running
  agents guidance + Claude Code primitives (verified against official docs).
  Nine dimensions, audit workflow, refactor playbook.
