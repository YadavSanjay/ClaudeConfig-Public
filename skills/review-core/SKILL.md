---
name: review-core
description: >-
  Evidence-backed, advisory code review of a change (working tree, branch, commit range
  or PR). A fresh-context reviewer works from a script-built review packet; a refuter
  tries to disprove every P0/P1 finding; deep effort (only for TRUNK, cross-subsystem or
  contract changes) adds consumer-impact reviewers driven by the caller tree, base-vs-change
  differential tests, and a red team that leaves proof tests behind. Reports P0–P3 severity, evidence tier
  (PROVEN/TRACED/INFERRED) instead of a confidence %, measured blast radius
  (LOCAL/MODULE/CROSS/CONTRACT), a validation ledger of what was and wasn't checked, and a
  machine-readable JSON report for future gating. Loads a project lens pack from the
  repo's .claude/review/ folder when present (domain lenses such as QuantLib numerics or RAG/LLM
  flows). Use when asked for a "deep", "thorough", "evidence-backed", "adversarial" or
  "pre-merge" review, a review "with blast radius", or by name (/review-core).
  Complements the built-in /code-review; does not replace it.
---

# review-core — accountable code review

Args: `[quick|standard|deep] [target] [--repo <path>]`. Defaults: `standard`, the cwd repo.
Target: nothing, `<rev>`, `<a>..<b>`, `--staged`, or a pull/merge request:
`--pr <url|number>`, which accepts a GitHub `…/pull/N` URL, a GitLab `…/-/merge_requests/N`
URL, or a bare number (resolved against `origin`).
- **The fetch.** `prep` fetches the PR head and its base from `origin` (no `gh`/`glab`
  needed). It checks that the URL's repo matches `origin`, then reviews `merge-base..head`
  from a **temp worktree**. The user's checkout and branch are never touched.
- **PR code is not executed by default.** The gate and `deep` differential tests are
  skipped for `--pr` (the ledger says so). Pass `--run-pr-code` only for a PR whose author
  you trust, such as your own branch: pytest imports its conftest and tests.
- **The pack comes from your checkout, never from the PR.** The PR's own `.claude/review/`
  is replaced by yours, or dropped if you have none. A PR can't suppress its own findings
  or change the gate.
- **Agents read the PR code from `review_repo`** in the summary, not the user's checkout.
- **`report` removes the worktree** (`--keep-worktree` keeps it). Reports go to the user's
  checkout, in `.claude/review/out/`.
- **Base branch.** Pass `--base <branch>` when the PR doesn't target the default branch.
- **Already merged PRs** are reviewed against their pre-merge base; the summary says so.
- **Intent.** Write it from the PR's `commits` list in the summary.

**Advisory mode.** Never edit the reviewed repo or change its git state. The one exception is
`<repo>/.claude/review/out/` (gitignored), where reports go. Offer fixes afterwards; apply them
only if asked.

| File | What it is |
|---|---|
| [contract.md](contract.md) | The standard: scales, finding schema, report template, ledger |
| [rubric.md](rubric.md) | The condensed contract; `prep` embeds it in the packet, so don't paste it |
| [agents.md](agents.md) | Reviewer, refuter and red-team briefs |
| [lenses/](lenses/) | The global `change-integrity` lens |
| [project-pack.md](project-pack.md) | How a repo adds `profile.md` and domain lenses |
| [scripts/review.py](scripts/review.py) | `prep` and `report`, all the deterministic review work |
| [scripts/codemap.py](scripts/codemap.py) | Code graph (transitive callers, test selection) and the TRUNK/BRANCH/LEAF code map |

## Effort

| Level | Reviewers | Extra evidence | Refuter | Red team |
|---|---|---|---|---|
| `quick` | you, inline, reading the packet (ledger: fresh context: no) | — | inline, P0–P1 only | — |
| `standard` | **1** fresh breadth agent covering all selected lenses | — | 1 agent, **only if P0–P1** | — |
| `deep` | 1 breadth agent **+ 0–3 consumer-impact agents** (one per downstream subsystem from the caller tree), launched together | **differential tests**: the selected tests run on base vs change | 1 agent, only if P0–P1 | 1 agent: proof tests + targeted attack |

**`deep` is gated.** `prep` keeps deep only when a touched file is TRUNK, reach is
CROSS/CONTRACT, or a contract surface is touched. Otherwise it **falls back to `standard`**
and says so in the summary and the report header ("deep requested → standard"). Follow the
`effort` that `prep` prints, not the one requested. If the user insists ("force deep"), pass
`--force-deep`. Suggest `deep` (never auto-run it) when a standard report says "consider
`deep`".

Proof budget per agent: quick 0 · standard 3 · deep 8 commands. All agents use
`model: sonnet` and run in the **foreground** (`run_in_background: false`). The run needs
their output to finish, and a background agent can outlive an orchestrator that is itself a
subagent.

## Procedure

`$W` is a scratch work dir. Run Python with the project's command, e.g. PowerShell
`conda run --no-capture-output -n py312 python <skill>/scripts/review.py …`.

1. **Intent:** write one line from the commit messages, the PR body or the user's request.
2. **Prep:** run `review.py prep --repo <repo> [--target …] --intent "…" --effort <level>
   --work $W`. Give the shell **at least a 10-minute timeout**, because the gate runs tests;
   a background shell slows it down.
   - It resolves the scope and selects lenses.
   - It runs the pack's gate on the tests that import the changed modules.
   - It builds the transitive blast map, with the code-map tier of each touched file.
   - It writes `$W/packet.md`.
   - At effective `deep`, it also writes one `packet-impact-<n>.md` per consumer target and
     runs the differential tests (`$W/diff_tests.json`). Git state stays untouched; the base
     is a `git archive` snapshot.
   - It prints a summary: effective effort and the deep verdict, impact targets, the
     differential-test line, lenses, gate exit codes, reach, tiers, code-map status and
     packet sizes.
   - Gate failures are recorded, not turned into findings.
   - A missing or stale code map is reported, not fatal. Mention it, and offer the Code map
     workflow below.
   - Over ~3,000 changed lines: review by top-level module and pass the uncovered parts as
     `--skipped` in step 5.
3. **Review:** run `review.py start --work $W`, then spawn the reviewer(s) using
   [agents.md](agents.md). The start mark times the review itself, for the cost record.
   - At `deep`, launch the breadth reviewer (`packet.md`) and every impact reviewer
     (`packet-impact-<n>.md`) **in one message**.
   - Save each JSON result as `$W/reviewer-<n>.json`.
4. **Refute and red-team:**
   - **Refute**, only if any finding is P0 or P1: spawn the refuter with just those findings
     (merged across reviewers) and save its result as `$W/refuter.json`.
   - **Red team** (`deep` only, after the refuter):
     - Part A writes proof tests to `$W/proofs/test_*.py`.
     - Fold its upgrades into the reviewer JSON.
     - Save its PROVEN-only new findings as another `--findings` file.
5. **Report:** run `review.py report --work $W --findings $W/reviewer-1.json […]
   [--refuter $W/refuter.json] [--skipped "what — why"]`.
   - The report is written to `<repo>/.claude/review/out/`, with or without a pack. The
     folder ignores itself (its own `.gitignore` contains `*`), so it never shows up in
     `git status`. Don't redirect it to a scratchpad with `--out-dir`: another session
     can't find a report there.
   - At `deep`, the ledger adds the impact reviewers, the differential-test verdict, and the
     proof tests as proposed regression tests.
   - Print its stdout as the answer, plus any stderr warnings that matter.
   - Don't hand-edit the report.
   - **Cost:** always pass `--cost "reviewer=<n>,refuter=<n>,…"`, using each Agent result's
     `subagent_tokens`. The ledger then records the review's cost; without it the run is
     marked `cost_missing`.
   - **History:** `report` also records the run and its findings in
     `review-history/` under the config directory (see Outcomes below). A `quick` run is recorded as `author-quick`, and
     any other effort as `independent`. Pass `--kind test` for synthetic or trial runs, which
     never count. Re-running `report` on the same review, such as a dry run and then
     `--post`, updates the same record.
6. **Post to the PR (GitHub; only when the user asked):** add `--post-dry-run` to show what
   would be sent, then `--post`. Use `--title-prefix` only if requested.
   - **What gets posted:**
     - one summary comment, edited in place on re-review;
     - inline comments only for P0/P1 on diff lines, as a COMMENT review (never
       approve/request changes);
     - labels: `ai-verdict:*`, `review:trunk`, `review:contract`, `review:deep`.
   - **On re-review,** findings that have gone are replied to and their threads resolved.
     Resolving needs repository write access. With a read-only agent token (Contents: read,
     Pull requests: write), the reply is posted and the report says which threads the human
     must resolve on GitHub.
   - **Token needs:** posting works with Pull requests: read and write; Contents can stay
     read-only. The agent never pushes: the human pushes the branch, then the agent may open
     the PR (`gh pr create --head <branch>`) and post.
   - **Refusals:** posting is refused when the PR head moved since the review
     (`--allow-stale` overrides), and for GitLab (not supported yet).
   - **The PR is** the `--pr` target, or the current branch's PR, or `--pr-number N`.
7. **Close.** Who fixes depends on where the review ran.
   - **The same session that wrote the code** (a `quick` round in the author loop): offer
     a fix in one line, P0/P1 first.
   - **A fresh session** (the final review, or a review of someone else's change): don't
     offer to fix. A reviewer that fixes its own findings ends up checking its own work.
     Instead, hand the fixes back to the authoring session with a ready prompt to paste
     there. Cite the report **repo-relative** and by its timestamped name, e.g.
     *"Address findings 1, 3 from `.claude/review/out/<timestamp>-<sha>.json`; reply
     'disputed' with a reason for any you reject."* Not `latest.json`, which the author's
     next `quick` round overwrites. Name the findings that need a human decision rather
     than a code fix.
   - **Either way,** offer to add rejected findings to `suppressions.md` with reason
     `false-positive|accepted-risk|wontfix`. Only a human adds them (see Author loop).

**Talking about findings later** (fixing, reporting progress, handing over): never refer to
findings by number alone; the user can't see the report. Run
`python scripts/review.py status --repo <repo> [--report <json>]` and show its table. It lists
the number, severity, location, a one-line problem and fix, and a status for each finding:
- **open:** the cited code is the same as when it was reviewed;
- **changed — recheck:** the cited code has been edited since;
- **unknown:** this checkout doesn't contain the reviewed commit (a PR you haven't checked
  out, another branch), or the report predates fingerprints;
- **suppressed:** with the reason from `suppressions.md` (`--all` lists these too).

`report` stores a fingerprint of each finding's cited code (`code_fp`), and `status`
compares the current code against it. This works the same for working-tree, commit and PR
reviews.

Show it before proposing fixes and again after making them. Status reads the code; only a
fresh review marks a finding fixed.

## Outcomes, labels and the monthly retro

This is the data a future gate will rest on. Per-lens precision needs to know what happened to
each finding, and recall needs to know which bugs got through. Design:
[design/outcomes-and-retro.md](design/outcomes-and-retro.md).

- **Between review rounds,** inference is automatic. When a later review of the same PR or
  branch no longer reports a finding:
  - if its cited code changed, it is recorded as `fixed`;
  - if its cited function is gone, it is recorded as `superseded`;
  - if the code is unchanged, it is left `unlabelled` and flagged.
- **When the user says a PR merged:** run `review.py outcomes --repo <repo> --pr <n>`.
  - It shows the last review's findings that still need an outcome, against the merged code.
    Changed code suggests `fixed`; unchanged code means shipped as-is.
  - With the user's go-ahead, `--accept-suggested` records the suggestions.
  - The user labels the rest. Run `review.py label --repo <repo> <finding> <outcome> "<reason>"`
    with their decision, using one of `fixed`, `false-positive`, `accepted-risk`, `wontfix` or
    `superseded`. `false-positive`, `accepted-risk` and `wontfix` also go into `suppressions.md`.
- **Only the human decides outcomes.** On your own judgement you may only run
  `label --by author <finding> disputed "<reason>"`; the human confirms or overrides it.
- **Monthly retro:** `outcomes` reminds you when the last retro was more than 30 days ago.
  Then run `review.py retro --repo <repo>`, which covers the last 30 days, or `--month <yyyy-mm>`.
  It reports:
  - per merged PR: review rounds, findings per round, P0/P1 left at merge, cost, and comments
    that didn't come from review-core;
  - **bug attribution candidates.** It finds fix commits in the period, blames the lines they
    changed back to the PR that introduced the bug (any lag; renames and reformats are
    followed), and classifies each as `missed`, `dismissed-real-bug`, `accepted-materialised`
    or `unreviewed`. A class matched only by function is marked provisional.
  - findings still unlabelled.

  It then compacts closed months.
- **Confirming a candidate:** show the user each one. With their decision, run
  `review.py escape --repo <repo> --fix <sha> --confirm --class <class>`. Use `--class
  not-a-bug` for a commit that wasn't a bug fix. For a bug with no fix yet, use `escape --file
  <path> --symbol <name> --pr <n> --note "…"`.
- **Precision:** `review.py precision [--repo-name R] [--lens L] [--severity P0,P1]
  [--window 12m | --since-lens-change]`. Per lens it shows true and false positives, labelled
  and unlabelled counts, and the Wilson 95% lower bound. Fewer than 40 labelled findings are
  reported as "insufficient".
- **Storage:** history is plain files under the config directory. It may name private code, so
  it stays out of public repos; follow the user's own rule for versioning it. Full detail is kept for 12 months; after that, aggregates plus ids and locations are kept,
  so attribution still works. `review.py history-import` backfills old reports.

## Author loop (iterating on review findings)

- **Rounds.** Iterate with `quick` (cheap, inline) and fix P0/P1 between rounds. Each round
  re-reviews the whole branch, so a regression introduced by a fix also shows up.
- **The final pre-MR review** runs at `standard` **in a fresh session** (new terminal or
  `/clear`), with the intent taken from the commit messages, not the author's framing. That
  report is what gets posted.
- **Fixes from the final review go back to the authoring session.** Hand over the
  repo-relative path of the JSON report, not a copied table: the JSON has the full failure, fix and proof text. Check
  the fixes with a `quick` round there. Run one more fresh `standard` only if the fixes were
  substantial.
- **The loop ends when no P0/P1 remains, not when a review comes back empty.** A fresh
  reviewer always finds some P2/P3, so "zero findings" never converges. P2/P3 are fixed or
  accepted at the author's discretion; a SHIP-WITH-NOTES verdict can merge.
- **The author never suppresses its own findings.** It marks them *disputed* in its reply;
  only a human adds them to `suppressions.md`. Self-suppression would corrupt the precision
  data that gating depends on.
- **Escalate** if P0/P1 findings survive 3 rounds. That's usually a design problem, so stop
  and bring in a human.

## Code map (one time per repo; refresh after broad refactors)

TRUNK / BRANCH / LEAF reflects what the system is *for*, so it comes from a committed
declaration that you approve. Nothing is inferred at review time. See
[project-pack.md](project-pack.md) for the keys and rules.

1. Run `scripts/codemap.py scan --repo <repo>`. It lists the CLAUDE.md files, design docs,
   routes, CLI modules, top modules by PageRank, and the current declaration.
2. Read those CLAUDE.md files and design docs. Draft `criticality`:
   - `outputs`: what the system produces;
   - `data_edges`: file-mediated flows;
   - `non_value`;
   - `pins`.
   Show it to the user, or show a diff against the existing one, and call out the judgement
   calls.
3. Only after approval, write it into `profile.md` and run `codemap.py build --repo <repo>`.
4. Present the tier counts, the infra and pinned modules, and samples of each tier. Turn
   disagreements into `pins` or declaration edits and rebuild. `codemap.py explain <path>`
   shows one module's reasoning.

## Principles

- **Evidence over assertion.** No confidence percentages. An INFERRED finding says what
  wasn't verified.
- **Deterministic work lives in `review.py`.** Don't redo by hand what it computes (scope,
  blast map, gate, report).
- **Coverage is part of the result.** The ledger always shows what was read, run and
  skipped.
- **The contract is stable.** Changing a scale or the JSON shape means bumping `schema` in
  `contract.md` and mirroring the change in `rubric.md` (a test checks the scale names).

## Changelog
- 2026-09-30 — review-core/1: initial contract, 4 global lenses, advisory mode.
- 2026-09-30 — pilot on faultline: PROVEN now requires a demonstrated failure;
  ai-authored P3 anchor for coverage-overstating comments; brace globs allowed in `applies_to`.
- 2026-10-01 — refactor for cost and modularity:
  - Standard effort uses 1 reviewer; the refuter runs only for P0–P1.
  - Agents run on Sonnet in the foreground.
  - One script, `review.py`, with two subcommands: `prep` (scope, lenses, gate, blast map,
    packet) and `report`.
  - `correctness` + `ai-authored` merged into the gap-only `change-integrity` lens;
    `security` and `config-provenance` removed (on-prem projects).
  - Layout flattened from 19 files to 9; the profile gained manifest frontmatter.
  - `deep` is unchanged apart from the lens set and is pending its own redesign.
- 2026-10-01 — code graph and code map (`scripts/codemap.py`):
  - The blast map follows transitive callers through wrappers (3 hops) and reports the
    path that justifies its reach.
  - Tiers come from the committed `criticality` declaration, plus structural centrality
    and co-change.
  - The gate runs only the tests that import the changed modules, labels environment
    failures, and times out at 300 s.
- 2026-10-02 — `deep` redesign:
  - A qualification gate: deep stays deep only for TRUNK, CROSS/CONTRACT reach or contract
    surfaces; otherwise it visibly falls back to standard. `--force-deep` overrides.
  - Per-lens reviewers are replaced by 1 breadth reviewer plus 0–3 consumer-impact
    reviewers from the caller tree.
  - Differential tests: the selected tests run on a `git archive` base snapshot and on the
    change.
  - The red team writes proof tests to `$W/proofs/`; it attacks along the caller-tree paths.
  - The deep suggestion now also fires on CONTRACT reach.
  - Gate and diff runs set `PYTHONDONTWRITEBYTECODE`.
- 2026-10-02 — review quality:
  - `report` validates each cited line against the file and symbol span and relocates wrong
    ones (keeping `line_reported`, with a warning). Validation runs before merging, so the
    correct citation wins on an evidence tie.
  - The terminal report clips fields to 1–2 sentences and proofs by length; the JSON keeps
    the full text.
  - The `Skipped` list is de-duplicated and capped at 5.
- 2026-10-02 — test reach in the code map: each module gets `test_coverage`
  direct|indirect|none, where test-file reach follows imports transitively through helpers.
  `build` reports `trunk_test_coverage` and `trunk_untested`; the packet shows the coverage
  of each touched file. This replaces the misleading "without direct tests" list.
- 2026-10-02 — PR/MR targets without `gh`:
  - `--pr` takes a GitHub or GitLab URL, or a number. URLs are parsed strictly and checked
    against `origin`.
  - The head and base are fetched from `origin` and reviewed from a temp worktree, which
    `report` removes.
  - Merged PRs are diffed against their pre-merge base.
  - The `gh`-based `--pr N` path, which could not build a blast map, was removed.
- 2026-10-02 — posting and cost:
  - `report --post` / `--post-dry-run` publish to GitHub PRs: a summary comment (edited in
    place), inline P0/P1 comments as a COMMENT review, `ai-verdict:*`/`review:*` labels, and
    an optional `--title-prefix`.
  - Re-review resolves threads for findings that have gone.
  - A stale-head guard refuses to post when the PR moved since the review.
  - `--cost` records per-agent tokens; the ledger gains `tokens` and `wall_s`.
  - New "Author loop" guidance: `quick` iterations, a fresh-session final review, no
    self-suppression, escalation after 3 rounds.
- 2026-10-02 — fixes from review-core's own review:
  - The merge no longer collapses distinct defects. Ids match ignoring the lens prefix;
    different ids merge only across reviewers, on the same file, non-empty symbol and
    severity, within 5 lines.
  - `--pr` no longer runs PR code unless `--run-pr-code` is passed. Gate paths with shell
    metacharacters or spaces are left out and listed under `unsafe_paths_skipped`.
  - `prep` survives tracked files deleted in the working tree and untracked nested repos.
  - The diff parser pins `a/`/`b/` prefixes and unquotes git's C-quoted paths.
  - `prep` stores the changed-line map in `scope.json`, so `report` can be re-run after the
    PR worktree is gone.
  - Stale junit files are deleted before each differential test run.
  - A refuter "kept" may narrow blast or weaken evidence, never raise either; PROVEN stands.
- 2026-10-02 — the close step depends on where the review ran:
  - A fresh-session review no longer offers to fix its own findings. It hands them back to
    the authoring session as a report path plus a ready prompt.
  - The author loop adds the hand-off and a stop rule: the loop ends when no P0/P1 remains,
    not when a review returns zero findings.
  - `report` writes to `<repo>/.claude/review/out/` even without a pack, and the folder
    ignores itself. A hand-off path is then repo-relative, so the authoring session reads
    it directly instead of searching another session's scratchpad.
- 2026-10-02 — fixes from the second self-review:
  - PR reviews take the pack only from the user's checkout. The PR's own `.claude/review/`
    is replaced, or dropped when the user has none.
  - Re-posting replies "no longer reported" once per gone finding: our replies carry a
    state marker. A finding reported again gets a reply and its thread reopened.
- 2026-10-02 — `review.py status` and refuter hardening:
  - `status` prints a saved report's findings as a table, each marked open, changed since
    review (recheck) or suppressed. The close step and any later talk about findings use
    it, never bare numbers.
  - A refuter can only cut: `downgraded` can't raise anything, a downgrade that lowers
    nothing counts as kept, and an unknown verdict is warned about instead of crashing.
  - contract.md §6 now lists the ledger keys `report` actually writes.
- 2026-10-02 — fixes from a further self-review:
  - `status` compares a fingerprint of each finding's cited code (`code_fp`, stamped by
    `report`) instead of diffing against `head_sha`. Working-tree reviews no longer read
    as "changed" straight away. It reports "unknown" when the checkout doesn't contain the
    reviewed commit, such as a PR viewed from the base branch.
  - PR `report` re-runs read suppressions from the user's checkout, so a dry run followed by
    `--post` no longer loses them. Location checks are skipped, with a warning, once the
    worktree is gone.
  - A PR `report` re-run fingerprints the reviewed head from the user's checkout, so the
    posted report keeps `code_fp`. The ledger says "fresh context: no" for `quick`.
- 2026-10-03 — read-only agent token: posting was verified with a fine-grained token
  (Contents read-only, Pull requests read and write). Everything posts except thread
  resolution, which needs repo write. The report now says which threads the human must
  resolve, instead of claiming they were resolved.
- 2026-10-03 — outcomes, bug attribution and the monthly retro (`scripts/history.py`;
  design in `design/outcomes-and-retro.md`):
  - `report` records every run in `review-history/` under the config directory. Re-runs update the same
    record. Runs are `independent`, `author-quick` or `test`, and carry tokens, review time,
    skill version and model. The report schema is now `review-core/2`, and its `repo` is the
    origin repo's name, not the PR worktree's.
  - Outcomes are inferred between rounds (`fixed`, `superseded`, or `unlabelled` and
    flagged), suggested at merge (`outcomes --pr`), and labelled by the human (`label`).
  - `retro` attributes later bug fixes to the PR that introduced them, at any lag, and
    reports process stats. `escape` confirms the attributions; `precision` reports Wilson
    bounds; `compact` keeps 12 months of detail; `history-import` backfills old reports.
- 2026-10-03 — fixes from the outcomes review:
  - Outcome file names are much shorter (≤ ~110-character repo paths, for Windows path
    limits); `compact` renames old ones. PR worktrees are created with `core.longpaths`.
  - Only branch and working-tree reviews chain as rounds. A review of an explicit commit or
    range never infers outcomes for another run.
  - `escape --pr` recomputes the class and covering findings for the overriding PR.
  - `outcomes --pr` refuses when the merge commit isn't available locally, instead of
    suggesting `superseded` for everything.
  - A history error no longer costs a review its report; it becomes a loud warning.
  - Confirming a provisional attribution requires `--class`.
- 2026-10-04 — Phase A housekeeping:
  - `render()` and the merge-outcomes table escape `|` and newlines in table cells.
  - The code map is no longer reported stale just because a review added untracked files.
  - The run id is one per `prep` (`prepared_at`), so adding a red-team findings file to a
    re-run of `report` updates the same run. `compact` merges older duplicates.
