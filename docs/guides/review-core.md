# review-core: a guide for first-time readers

review-core is a Claude Code skill that reviews a code change and writes a report about what
might break. You run it as `/review-core`. This page explains why it exists, how it works, when
you can rely on it, and what it doesn't do yet.

For the exact rules, see the skill's own files:
- [SKILL.md](../../skills/review-core/SKILL.md) for the operating steps;
- [contract.md](../../skills/review-core/contract.md) for the scales and report format.

## Why we do this

Most code in these projects is now written by AI agents. A plain "please review this" to another
model has three problems:

1. **You can't tell how sure it is.** A model saying "90% confident" is not a measured number.
2. **You can't tell what it looked at.** "No issues found" might mean it checked everything, or
   that it read one file.
3. **You can't tell whether it's any good over time.** Nobody records which findings were real
   bugs and which were noise, so you never learn whether to trust it.

review-core answers each one:

| Problem | What review-core does instead |
|---|---|
| Unmeasured confidence | Each finding states the **evidence** behind it: it was demonstrated by running something (PROVEN), followed through the code (TRACED), or only reasoned about (INFERRED). |
| Unknown coverage | Every report ends with a **validation ledger**: what was read, what was run, and what was skipped. |
| Unknown quality | Every review and its findings go into a **history**. You later mark each finding as fixed, a false positive, and so on, so the skill's precision can be measured. |

The long-term goal is to let review-core **block merges**, but only for the kinds of check that
have proven trustworthy on real data. Until then it is **advisory**: it reports, and you decide.

## How it works, in plain terms

```
your change ──► prep (a script) ──► reviewer agent ──► refuter agent ──► report (a script)
                 │                    (fresh context)    (only if serious)    │
                 └ works out what changed,                                    └ terminal summary,
                   who calls it, which tests                                    JSON file, history
                   cover it, and builds a "packet"
```

1. **Prep** is a deterministic script, not a model. It finds the changed files and lines,
   follows the code to see what calls the changed functions (up to 3 levels up), runs the fast
   tests that import the changed code, and writes everything into one file, the *packet*.
2. **A reviewer agent** starts with a clean context. It has never seen the conversation that
   wrote the code, so it can't inherit the author's assumptions. It reads the packet and the
   code, and returns findings as JSON.
3. **A refuter agent** runs only when a finding is serious (P0 or P1). Its job is to *disprove*
   each one. It can drop a finding or lower its severity, but never raise anything.
4. **Report** is a script again. It checks that each cited line really exists, merges
   duplicates, applies your suppressions, prints the summary and saves the JSON.

The scripts do every job that has a right answer: scope, blast radius, line checks, the report
layout. The models do only the judgement.

### What a finding tells you

| Field | Values | Meaning |
|---|---|---|
| Severity | P0 blocker, P1 major, P2 minor, P3 nit | What happens if this ships. P0: wrong results reach a user silently, or data is lost. P3: a cleanup a senior engineer would still mention. |
| Evidence | PROVEN, TRACED, INFERRED | How we know. Not a percentage. |
| Blast radius | LOCAL, MODULE, CROSS, CONTRACT | How far the damage could reach, **measured** from the caller tree. CONTRACT means it crosses a lasting boundary, such as saved data, a public API or a config format. |

The **verdict** follows from the worst finding: BLOCK (any P0), FIX-FIRST (any P1),
SHIP-WITH-NOTES (only P2/P3), or SHIP (nothing found).

### Effort levels

| Level | What runs | When to use it |
|---|---|---|
| `quick` | The current session reviews inline. No fresh reviewer. | While you're still writing: cheap rounds between fixes. |
| `standard` | One fresh reviewer, plus the refuter if anything is P0/P1. | The normal final review before a PR merges. Run it in a **new** session. |
| `deep` | Adds reviewers for each downstream consumer, runs the tests on the old and new code to compare, and a "red team" that writes tests trying to prove the bugs. | Only for changes to the core of a system. `prep` refuses deep for small changes and falls back to `standard`, saying so. |

### Project packs: teaching it about a repo

On its own, review-core uses one general lens, `change-integrity`: broken callers, things that
must change together but didn't, weakened tests, claims in docs that the code doesn't back up.

A repo can add a folder, `.claude/review/`, with:
- **a profile:** how to run Python and the tests there, and which files are contracts;
- **domain lenses:** checklists specific to that code, for example pricing numerics or RAG flows;
- **suppressions:** findings you've already dismissed, so they don't come back;
- **a code map:** which files are TRUNK (what the system exists to produce), BRANCH or LEAF. You
  approve this map once; it's not guessed at review time.

The pack is always read from **your** checkout. A PR can't change its own review rules.

## How to use it

```
/review-core                      # standard review of your current branch vs main
/review-core quick                # cheap inline pass while you iterate
/review-core standard <PR URL>    # review a GitHub PR (fetched into a temporary folder)
/review-core deep                 # deep, if the change qualifies
```

A typical loop for a change:

1. While writing, run `quick` a few times and fix P0/P1 findings.
2. When you think it's ready, open a **new session** and run `standard`. A reviewer in the same
   session would be checking its own work.
3. Take the fixes back to the session that wrote the code. The review session gives you a
   ready-made prompt that points at the saved JSON report.
4. Stop when **no P0/P1 remain**. Don't chase zero findings: a fresh reviewer always finds some
   minor ones.
5. Optionally, post the summary to the PR (`report --post`). The AI comments; it never approves.
6. After merge, label the outcomes (`outcomes --pr N`, then `label`), so precision can be
   measured.

Reports are saved in `<repo>/.claude/review/out/`, which git ignores. To discuss findings later,
use `review.py status`. It shows each finding as **open**, **changed since review** or
**suppressed**, so nobody has to refer to "finding 3" from memory.

## When it works well

- **Python repos with a project pack:** blast radius, the test selection and the code map all
  work fully there.
- **Changes up to a few thousand lines:** the packet fits comfortably, and one reviewer can read
  everything relevant.
- **GitHub PRs:** fetching, reviewing from a temporary folder, posting and re-posting have all
  been used on real PRs.
- **Docs and config changes:** reviewed with the general lens, which catches contradictions and
  claims that don't hold.

## Edge cases to be aware of

- **PR code isn't run by default.** For a PR, the tests are skipped, because running them would
  execute someone else's code on your machine. Pass `--run-pr-code` only for a PR you trust,
  such as your own.
- **Low-confidence nits are dropped by rule.** A P3 with only INFERRED evidence never appears in
  the report. That is deliberate, not a bug.
- **Wrong line numbers are corrected silently, apart from a warning.** If a reviewer cites a
  line that doesn't exist, `report` moves the citation to the start of the cited symbol, or to
  line 1 if the file has no symbol, and prints a warning. Check the warnings when a location
  looks odd.
- **Gate failures aren't findings.** If the tests fail during `prep`, the report records it, but
  doesn't turn it into a finding. A test that needs data you don't have shows as an
  environment gap.
- **A missing or stale code map** is reported, not fatal. Without one, every file is UNMAPPED and
  `deep` has less to go on.
- **Over about 3,000 changed lines,** review by module and list what wasn't covered.
- **Resolving review threads on GitHub needs write access,** which the agent's token doesn't have.
  The agent replies to a fixed finding's thread; you resolve it.
- **The author never suppresses its own findings.** It can mark one *disputed*. Only you add
  suppressions, otherwise the precision data would be meaningless.
- **If P0/P1 findings survive three rounds,** stop and look at the design. That usually means
  the problem isn't a line of code.

## Not implemented yet

- **Gating (blocking merges).** The report format is ready for it, but no check reads it yet.
  A lens has to earn it first: at least 40 labelled findings from 10 or more PRs, and a
  precision lower bound of 0.80.
- **Real project data.** Most of the history so far is review-core reviewing its own config
  repo. No project lens has enough labelled findings to judge it yet.
- **GitLab:** reviewing a merge request by URL is unit-tested but unused; posting to GitLab is
  not built.
- **A `tdd` lens** that spots code fitted to tests (planned with the tdd-spec skill).
- **Archiving old history:** built and tested, but no month has closed yet, so it hasn't run on
  real data.

## Words used on this page

| Term | Meaning |
|---|---|
| Packet | The single file `prep` builds for the reviewer: the diff, the caller tree, the rules and the project profile. |
| Lens | A checklist for one kind of defect. `change-integrity` is the global one; packs add domain lenses. |
| Blast map | The list of code that calls the changed functions, used to measure blast radius. |
| Refuter | An agent whose only job is to disprove serious findings. |
| Suppression | A dismissed finding, recorded with a reason, so it isn't raised again. |
| Precision | Of the findings a lens raised, the share that were real. Measured from your labels. |
| Escape | A bug found after merge and traced back to the PR that introduced it. It measures what reviews **miss**. |
