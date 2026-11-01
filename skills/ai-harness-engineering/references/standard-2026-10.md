# AI Harness Engineering — the standard (rev 2026-10)

> Dated standard. Everything here is provisional and expires as models and tooling
> improve. When you revise, copy this file to `standard-<new-date>.md`, edit the copy,
> and update `SKILL.md`'s changelog. Never overwrite a dated file in place. The previous
> revision is [standard-2026-07.md](standard-2026-07.md).

Each dimension has (a) the principle, (b) a checklist scored `Absent / Partial / Solid`,
and (c) the failure mode it prevents. Product facts (event counts, limits, flags) are
current as of 2026-10 and drift fast: verify them against the docs before quoting them.

---

## The through-line (read first)

1. **Build reactively, then prune.** Add a harness component in response to an
   *observed* agent failure, record why it exists, and give it a *retire-when* condition.
   "Every component in a harness encodes an assumption about what the model can't do on
   its own" (Anthropic, 2026-03). Assumptions go stale with each model release.
2. **Enforce with executable controls, not prose.** A rule that lives only in an
   instruction file is a suggestion. Hooks, linters, tests, CI and sandboxes are rules.
3. **Contain at the environment layer first, then steer at the model layer.**
   Model-layer defences (classifiers, instructions, approval prompts) are probabilistic.
4. **Measure the harness, not only the product.** Know which hooks block, which skills
   fire, what each component costs, and whether it beats a no-component baseline.

---

## 1. Context & memory architecture

**Principle:** the agent is amnesiac between sessions; the filesystem is the memory. The
context window is the scarcest resource, and performance degrades as it fills.

Checklist:
- [ ] The instruction file (`CLAUDE.md`, or `AGENTS.md` when several agent tools share the
      repo) is **under ~200 lines**, acts as a **table of contents**, and holds only what
      the agent can't infer from the code: commands, non-default conventions, gotchas,
      repo etiquette. Each line passes "would removing this cause a mistake?". Audited
      periodically (`/doctor` prompt audit or equivalent). Human-written, not generated.
- [ ] Knowledge that applies to part of the codebase lives in **path-scoped rules**
      (`.claude/rules/` with `paths:`) or module-level instruction files; procedures live
      in **skills**, loaded on demand.
- [ ] Long-running work keeps a durable **plan / progress file** so a fresh session
      rebuilds state from disk.
- [ ] Prefer **`/clear` plus a written handoff** to a fresh session over carrying a long,
      corrected context. Compaction instructions (what must survive) are written down;
      critical rules never depend on surviving compaction.
- [ ] **Auto memory** and other agent-written memory are reviewed for staleness and
      poisoning; they are context, not configuration.
- [ ] Secrets live in a gitignored `.env` or secret store, never in instruction files.

Prevents: context rot, ignored rules in a bloated file, work lost on reset, stale or
poisoned memory steering the agent.

## 2. Planning (spec before build)

**Principle:** turn a vague ask into a verifiable specification before writing code, and
make "done" external and machine-checkable.

Checklist:
- [ ] A **feature / requirements list** with testable per-item criteria that start failing.
- [ ] A **scope document** (`VISION.md` or equivalent) the agent declines against.
- [ ] Larger features: an **interview → `SPEC.md` → fresh implementation session** flow.
      The spec names files and interfaces, states what is out of scope, and **ends with an
      end-to-end verification step**.
- [ ] Plan mode is used **when the diff can't be described in one sentence**; small,
      clear changes skip it (planning has overhead).
- [ ] For long autonomous builds only: a **planner** role that expands the ask (without
      one, generators under-scope). Sprint contracts and boot scripts are optional; current
      models rarely need them.

Prevents: premature "done", scope creep, solving the wrong problem.

## 3. Generation / execution

**Principle:** keep the coordinator's context clean; push work into isolated workers and
hold intermediate state outside context.

Checklist:
- [ ] Investigation and verbose operations go to **subagents** that return only a summary.
      Each subagent prompt names the **files, the question, the permissions and the
      evidence to return**.
- [ ] Every subagent has an **explicit, minimal tool list** and an appropriate model.
- [ ] Large fan-out (audits, migrations, cross-checked research) uses **dynamic
      workflows**: a script holds the loop and the intermediate results; runs are bounded
      (default 16 concurrent agents, 1,000 per run), resumable, and savable as commands.
- [ ] Agents that **mutate files in parallel** use **worktree isolation**.
- [ ] Parallelism is used for **independent work**, not role-play pipelines: evidence for
      large role-based agent teams is weak (a four-role team ≈ the best single agent;
      chains of more than four handoffs mostly fail). Agent teams are experimental.

Prevents: context pollution, lost intermediate state, parallel-write conflicts,
over-privileged agents, coordination overhead with no quality gain.

## 4. Evaluation — doer ≠ judge

**Principle:** the agent doing the work must not be the only judge of it. Give it a check
it can run, and choose how hard that check gates the stop.

Gate ladder (cheapest first; pick per task):
- [ ] **Runnable check in the prompt:** a test, build, linter, fixture diff or screenshot
      the agent runs and iterates on. The agent **shows evidence** (command + output), not
      an assertion of success.
- [ ] **`/goal` condition** across a session: a fresh small model judges a measurable end
      state from the transcript after every turn (no tools — phrase conditions as things
      the agent's own output demonstrates; add a turn or time bound).
- [ ] **Deterministic Stop gate:** a Stop hook that blocks the turn on a red build. A
      guarantee, not an opinion.
- [ ] **Second opinion, when warranted:** a fresh-context reviewer subagent or a
      self-checking workflow, with no write tools and a default-FAIL contract. Use it
      **when the task sits beyond what the model does reliably solo**, not on every change.
      Calibrate it (graded criteria, few-shot examples, read its logs), and instruct it to
      report only gaps that affect correctness or stated requirements, so its findings
      don't drive over-engineering.

Product evals (for AI/RAG systems):
- [ ] **Deterministic graders where possible, LLM graders where necessary**, humans to
      calibrate LLM graders.
- [ ] **Capability vs regression** suites are separate; capability evals start low and
      graduate to regression. Saturated evals (≈100%) give no improvement signal.
- [ ] **Noise is measured:** repeated runs, a reported spread, pass@k or pass^k chosen
      deliberately, infrastructure held constant. A near-zero pass rate across trials
      usually means a broken task.
- [ ] **Transcripts are read**, not only scores.

Prevents: models overrating their own work, plausible-but-broken output, grader drift,
chasing noise.

## 5. Deterministic guardrails (hooks and CI)

**Principle:** if it must always happen, wire it; don't ask nicely.

Checklist:
- [ ] **PreToolUse guard** for protected paths, secrets and boundaries; security hooks
      **fail closed** (`onFailure: "block"` or equivalent).
- [ ] **PostToolUse lint / format** where the repo has a formatter or linter.
- [ ] **Stop gate** (see Dimension 4).
- [ ] The **same guards run in CI / git hooks**, because not every developer or agent runs
      the same harness.
- [ ] **Every guard is tested both ways:** cases it must block and cases it must allow.
- [ ] Hooks are wired in settings (not loose files), reviewed as security-sensitive code,
      and use the handler type that fits (command, http, mcp_tool, prompt, agent).
- [ ] No security rule exists **only as prose**.

Prevents: secret leaks, protected-file damage, unsafe stops, rules that silently don't
apply.

## 6. Containment & permissions (new in rev 2026-10)

**Principle:** cap the blast radius by limiting what the agent *can* reach, not only by
supervising what it does.

Checklist:
- [ ] Unattended or broad-autonomy work runs inside an **OS sandbox, devcontainer or VM**.
      (The Claude Code Bash sandbox runs on macOS, Linux and WSL2, not native Windows.)
- [ ] **Network egress is denied by default**; every allowlisted domain is treated as a
      capability grant.
- [ ] **Credentials never enter the sandbox** or the workspace the agent edits (brain /
      hands separation; inject through a proxy or the host).
- [ ] Permission allowlists are **narrow**; no broad interpreter or `Bash(*)` rules
      (they bypass classifiers).
- [ ] Classifier-based approval (auto mode) and human approval prompts are **layers, not
      the boundary** (auto mode misses ~17% of risky actions; users approve ~93% of
      prompts).
- [ ] Repos, READMEs, tool outputs and MCP results are **untrusted input**; project-local
      config (hooks, settings) runs only after the trust prompt.
- [ ] Tool permissions are scoped (read-only data access by default).

Prevents: exfiltration, prompt-injection-driven actions, approval fatigue standing in
for security.

## 7. Prompt & tool hygiene

**Principle:** prompts and tools are versioned, testable assets.

Checklist:
- [ ] Prompts live in a **versioned registry**, not inline at call sites.
- [ ] Every external dependency sits **behind an interface with a test fake**; the suite
      runs with no network and no heavy models.
- [ ] LLM output that feeds code is **schema-validated**, with bounded retries.
- [ ] The **tool surface is minimal**: remove tools the agent doesn't need; prefer CLI
      tools over MCP servers for context efficiency; strong models often do well with a
      shell alone.
- [ ] Untrusted text reaches prompts as **data** (e.g. JSON-encoded in the user message),
      never in the system prompt or a format template.

Prevents: untestable pipelines, prompt drift, brittle parsing, prompt injection, tool
overload.

## 8. Cost tracking & controls

**Principle:** accounting and hard ceilings, and know which of your caps are which.

Checklist:
- [ ] **Hard caps** on autonomous runs: a budget ceiling (e.g. `--max-budget-usd` for
      headless runs, a per-run budget in your own gateway), runtime agent caps.
      **Advisory limits** (a `/goal` turn clause, a "large workflow" warning, a workflow
      size guideline) are labelled as advisory.
- [ ] **Per-call cost telemetry** priced from config, surfaced per run; usage attribution
      by skill, subagent, MCP server and scheduled loop; export (e.g. OpenTelemetry).
- [ ] **Model tiering:** a cheap default with escalation on a quality signal, or a cheap
      executor plus a **stronger advisor** consulted at decision points.
- [ ] **Circuit breaker** on consecutive failures; truncation and halts are logged.
- [ ] Harness cost is justified against benefit (a full multi-agent harness can cost ~20×
      a solo run); background spend (scheduled tasks, goal check-ins, idle teammates) is
      accounted for.

Prevents: runaway spend, invisible cost, top-tier-for-everything waste.

## 9. Autonomy safety & recovery

**Principle:** unattended runs need an escape hatch, a watchdog and a written policy.

Checklist:
- [ ] **Rollback:** git is the real rollback. Checkpoints / `/rewind` restore only edits
      made through the file tools, not Bash or external processes.
- [ ] **Runaway protection:** a watchdog for orphaned or runaway processes and tight retry
      loops (platform-specific).
- [ ] **Observability:** decisions, tool calls, escalations, denials and cost are
      reviewable after the fact.
- [ ] **Unattended-run policy:** what may run headless or scheduled, under which
      permission mode (`dontAsk` with explicit allowed tools, or auto mode) inside which
      sandbox, with which caps, and where alerts go.

Prevents: unrecoverable runs, zombie processes, unexplainable outcomes.

## 10. Harness evolution & measurement

**Principle:** the harness doesn't shrink so much as move; keep only what is still
load-bearing, and prove it.

Checklist:
- [ ] Every component carries a **retire-when** condition and the failure that justified it.
- [ ] After each major model release, **remove components one at a time** and keep what
      still pays (all-at-once simplification tends to fail).
- [ ] **Harness usage is measured:** hook blocks, skill activations, subagent and workflow
      spend, session friction.
- [ ] Components are **evaluated against a no-component baseline** where possible
      (plugin / skill evals).
- [ ] New rules and skills are added **in response to observed failures**, not installed
      prophylactically.
- [ ] The standard is **dated** and re-audited on a cadence (and within ~2 weeks of a
      major model release).

Prevents: cargo-culted complexity, paying to maintain scaffolding the model has outgrown.

---

## Claude Code primitive cheat-sheet (verified 2026-10)

| Need | Primitive | Key property |
|---|---|---|
| Persistent conventions | `CLAUDE.md` / `AGENTS.md` | Loads every session; keep under ~200 lines |
| Scoped conventions | `.claude/rules/*.md` with `paths:` | Load only when matching files are touched |
| On-demand knowledge / procedures | Skills | Load when invoked; `disable-model-invocation` for side-effecting workflows |
| Agent-written notes | Auto memory | First 200 lines / 25 KB load each session; review it |
| Deterministic gates | Hooks | 33 lifecycle events; command / http / mcp_tool / prompt / agent handlers; `if` filters; `onFailure: "block"` |
| Isolated workers | Subagents | Own context, tools and model; return a summary |
| Large fan-out | Dynamic workflows (`ultracode` keyword, saved `.claude/workflows/`) | Script-held state; 16 concurrent by default; 1,000 agents per run; resumable |
| Session completion check | `/goal <condition>` | Small fast model, transcript-only, met / not met / impossible |
| Stronger model at decision points | Advisor tool | Cheap main model + strong advisor (experimental; Anthropic API) |
| Permissions | Auto mode, allowlists, `dontAsk` | Auto mode is the default interactive mode; classifier is one layer |
| OS isolation | Sandboxed Bash | macOS / Linux / WSL2; filesystem + network limits |
| Hard spend cap (headless) | `--max-budget-usd` | Stops the run at the ceiling |
| Usage / cost visibility | `/usage`, `/insights`, OpenTelemetry, `modelPricing` | Attribution by skill, subagent, MCP, loop |
| Testing harness pieces | `claude plugin eval`, skill-creator evals | Graded cases, compare against a no-plugin baseline |
| Instruction audit | `/doctor` prompt audit | Proposes cuts and finds conflicts |
| Scheduled / unattended | Routines, scheduled tasks, `/loop`, `claude -p` | Pair with caps and a sandbox |
| Rollback | `/rewind`, checkpoints | File-tool edits only; use git for the rest |

## Anti-patterns (score as red flags)

- **Generator grades itself** with no independent check anywhere.
- **Prose-only rules** for anything that must always hold, especially security.
- **Oversized or machine-generated instruction files.**
- **Unbounded autonomous loop:** no hard budget, no circuit breaker.
- **Invisible cost:** no per-run pricing or attribution.
- **Everything on the top-tier model**, or an always-on reviewer whose every finding is
  chased.
- **Per-action approval or a classifier as the only safety layer**; broad `Bash(*)` allows.
- **Credentials reachable** from where generated code runs.
- **Unrestricted subagent tools**; tool sprawl.
- **Prompts as inline strings**; **no test fakes**.
- **Generic skills or subagents installed** before any failure called for them.
- **Silent truncation** of coverage, steps or results.
- **Checkpoints treated as rollback** for Bash changes.
- **Radical, all-at-once simplification** instead of one component at a time.

## Sources (2026-10)

- Anthropic — Harness design for long-running application development (2026-03):
  https://www.anthropic.com/engineering/harness-design-long-running-apps
- Anthropic — Scaling Managed Agents: decoupling the brain from the hands (2026-04):
  https://www.anthropic.com/engineering/managed-agents
- Anthropic — How we built Claude Code auto mode (2026-03):
  https://www.anthropic.com/engineering/claude-code-auto-mode
- Anthropic — How we contain Claude across products (2026-05):
  https://www.anthropic.com/engineering/how-we-contain-claude
- Anthropic — Demystifying evals for AI agents (2026-01):
  https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Anthropic — Quantifying infrastructure noise in agentic coding evals (2026-02):
  https://www.anthropic.com/engineering/infrastructure-noise
- Anthropic — Effective harnesses for long-running agents (2025-11):
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Claude Code docs — best practices, /goal, workflows, hooks, memory, costs, advisor,
  sandboxing, plugin evals: https://code.claude.com/docs/llms.txt
- OpenAI — Harness engineering (2026-02): https://openai.com/en/index/harness-engineering/
- marmelab — The State of AI Harness Engineering 2026 (2026-09):
  https://marmelab.com/blog/2026/09/24/the-state-of-ai-harness-engineering-2026.html
- An Empirical Study of Harness Design for Coding Agents (arXiv 2609.20804):
  https://arxiv.org/abs/2609.20804
