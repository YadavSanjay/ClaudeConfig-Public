# Design: review outcomes, retrospectives and the data for gating

Status: **proposed, for review** (2026-10-03). Nothing is implemented yet.

**Decided:**
- History lives in ClaudeConfig `review-history/`.
- Full detail is kept for 12 months; aggregates are kept beyond that.
- Labels are asked for when a PR merges.
- Retro runs monthly.

## 1. Problem statement

review-core is advisory. The plan is to let it gate merges later, per lens, once a lens has shown
it is trustworthy. The contract defines that test as per-lens **precision**: the share of a lens's
findings that were real. Today we can't compute it, and we learn nothing about what reviews
**miss**:

1. **No outcome per finding.** We record what each review *found*, but not what happened next:
   fixed, disputed, a false positive, or accepted. Precision is meant to come from
   false-positive suppressions, and none have been written. So precision reads as 100%, which is
   meaningless. In this session most findings were fixed, and nothing recorded that.
2. **The data isn't durable.** Reports and `ledger.jsonl` sit in each repo's ignored
   `.claude/review/out/`, local to one machine. Several early reviews were written to session
   scratchpads that have since been deleted.
3. **The data is polluted.**
   - Test runs aren't marked; one example is a synthetic posting test.
   - Re-running `report` appends a duplicate ledger line. One review appears
     three times.
   - `wall_s` is measured from `prep`, so one review shows 19 hours.
   - `--cost` was passed on only some runs.
   - Author-session `quick` rounds aren't distinguished from independent reviews.
4. **Nothing is learned from merges or later bugs.** Two things go unrecorded:
   - how a PR's review went: its rounds, and which findings survived to merge;
   - which bugs got through review and were found later, whether days or months after merge.

   That second item is the only signal of what the review **missed** (recall).

### Goals

- Record a **durable, deduplicated** history of review runs and findings for every repo.
- Give every finding an **outcome**, inferred automatically where that is safe and labelled by a
  human where it isn't. Labels are asked for when a PR merges.
- **Attribute later bugs to the PR that introduced them, however late they surface**, and
  classify them as missed, dismissed, accepted-and-materialised, or unreviewed.
- **`review.py retro`, run monthly:** process insights plus bug attribution.
- **`review.py precision`:** per-lens precision with sample sizes and an honest lower bound,
  computed over a rolling window. This is the input to a future gate rule.
- **Bounded growth:** 12 months of detail, then compact aggregates.

### Non-goals

- Turning on gating. That is a separate decision, made once the numbers support it (§5).
- Measuring recall exactly. Escapes give a proxy, not a complete count.
- Analysing reviews from tools other than review-core, beyond reading human PR comments as a
  "missed" signal.

## 2. Principles

1. **Only a human labels a finding a false positive.** The author agent may mark a finding
   *disputed* with a reason; a human confirms it. This is the same rule as for suppressions:
   self-labelling would corrupt the precision that gating depends on.
2. **Inference is conservative.**
   - `fixed` is inferred only when the cited code changed **and** the finding is absent from the
     next review. Anything less certain is `unlabelled`, not guessed.
   - Bug attributions are always confirmed by a human before they count.
3. **Small samples are shown as small.** Precision is reported with its sample size and a Wilson
   95% lower bound, and the gate rule uses the lower bound.
4. **Recent behaviour matters most.** Each run records the skill version and the model, and
   precision is computed over a rolling 12-month window, or since the lens last changed.
5. **Collecting data must not add noise to code PRs.** History lives in ClaudeConfig, never in a
   project PR's diff.
6. **No conflicts between branches.** Live data is one file per event; closed months are
   compacted on `main` only.
7. **Test runs never count.** They are marked at creation, and every metric excludes them.
8. **Everything recorded is reproducible from the files.** No metric depends on a cache, a
   session or the GitHub API being reachable later.

## 3. Data model and storage

### 3.1 Records

**`run_id`** is a hash of (repo, head SHA, effort, the reviewers' input files). Re-running
`report` on the same review (a dry run, then `--post`) **overwrites** the same run instead of
appending a duplicate.

**A run record** has the current ledger fields, plus:
- `run_id`, `repo`, and `pr` (number or null);
- `branch`, `base_sha` and `head_sha`;
- `kind`: `independent`, `author-quick` or `test`;
- `fresh_context` and `reviewers`;
- `skill_version`, `model` and `lens_versions` (a hash of each lens file);
- `review_wall_s`, from the first reviewer spawn to `report`;
- `tokens`, which the procedure now requires.

**A finding record:** `id`, `finding_key`, `lens`, `severity`, `evidence`, `blast`, `file`,
`symbol`, `line`, `code_fp`, `summary` and `refuter`. `finding_key` is the repo plus the finding
id without its lens prefix, which is stable across runs.

**An outcome event** has these fields:
- `finding_key` and `run_id`;
- `outcome`, one of: `fixed`, `disputed`, `false-positive`, `accepted-risk`, `wontfix`,
  `superseded`, `unlabelled`, `dismissed-real-bug`, `accepted-materialised`;
- `source`: `inferred`, `human`, `author` or `attribution`;
- `reason`, `by` and `at`.

The latest event wins; earlier events remain, for audit.

**An escape record** (from attribution, §4.4) has these fields:
- `fix` (commit or PR), `introduced_by` (commit and PR), `files` and `symbols`;
- `class`, one of: `missed`, `dismissed-real-bug`, `accepted-materialised`, `unreviewed`;
- `lag_days`, `confidence`, and `confirmed_by`.

**Precision counts:**
- **True positive:** `fixed`, `accepted-risk`, `wontfix`, `dismissed-real-bug` and
  `accepted-materialised`. The defect was real, even if not acted on.
- **False positive:** `false-positive`, and `disputed` once a human confirms it.
- **Excluded, but counted and shown:** `superseded` and `unlabelled`.

### 3.2 Storage tiers (in `ClaudeConfig/review-history/`)

| Tier | Layout | Kept |
|---|---|---|
| **Hot** | `<repo>/runs/<run_id>.json`, `<repo>/findings/<run_id>.jsonl`, `<repo>/outcomes/<finding_key>.<n>.json`, `<repo>/escapes/<fix>.json`: one file per event, so no branch conflicts | The current and previous month |
| **Archive** | `<repo>/archive/<yyyy>-<mm>.jsonl.gz`: one compressed file per closed month, with all records, lossless | 12 months |
| **Aggregates** | `<repo>/aggregates.json`: per lens per month, the true and false positives, labelled n, excluded n, escapes by class, cost, review rounds, and the skill and model versions seen | Forever (a few KB a year) |
| **Expired** | Archives older than 12 months are rewritten without free text (`summary`, `reason`). Ids, keys, file, symbol, line, `code_fp`, outcomes and counts are kept. | Forever, small |

The expired tier keeps what attribution needs (§4.4), so a bug found more than 12 months later can
still be classified as `missed` or `dismissed-real-bug`. Only the text explaining the original
finding is gone.

**Size:** at about 50 reviews a week, 12 months of detail is roughly 15 MB in about 12
compressed archives, plus up to 2 months of loose files. Deleting files doesn't shrink git
history, which is why compaction is built in from the start.

**Compaction:** `review.py compact` runs with the monthly retro, on `main`. For each closed month,
it:
1. writes the archive and refreshes the aggregates;
2. verifies the archive round-trips exactly to the loose files;
3. only then deletes the loose files;
4. expires archives older than 12 months;
5. prunes local `out/` reports older than 90 days.

The full reports in each repo's `out/` are local and ignored, as now, and aren't part of the
history.

## 4. Collection

### 4.1 At `report` time (automatic)

- `report` writes the run and findings files, with `--kind` (default `independent`); synthetic
  findings files force `test`.
- It requires `--cost`: without it, the run is marked `cost_missing`.
- `review_wall_s` is computed from timestamps the procedure records when the first reviewer
  starts.

### 4.2 Between rounds (inferred)

**`review.py outcomes [--pr N | --branch B]`** walks the runs of one PR or branch in order:
- a finding present in run *k* and absent in run *k+1*, whose cited code changed in between,
  gets an inferred `fixed`;
- if it disappeared with **no** code change, it is `unlabelled` and flagged;
- a finding whose symbol was deleted or rewritten gets `superseded`.

### 4.3 When a PR merges: labels

When the human says a PR merged, the agent runs `outcomes --pr N` against the merged code:
- **Changed since review and no longer reported** → `fixed`.
- **Unchanged at merge** → *shipped as-is*. These need a label.
- **The prompt:** the human is shown only the findings still needing a label, usually a handful,
  in the `status` table format.
- **Labelling:** `review.py label <finding> <outcome> "<reason>"` writes the outcome event. For
  `false-positive`, `accepted-risk` and `wontfix`, it also appends to the repo's `suppressions.md`
  (the existing mechanism).
- **The author agent** may run `label <finding> disputed "<reason>"` and nothing else.
- **The retro nudge:** if the last retro was more than 30 days ago, `outcomes` says so and offers
  to run `retro` and `compact`.

### 4.4 Bug attribution: from the fix back to the PR (any lag)

Escapes are found by working backwards from the fix, not forwards from the PR, so a bug found
months after merge is attributed just like one found the next day. This is the standard SZZ
method.

1. **Fix sources:**
   - Fix commits made **during** the retro's period, regardless of when the bug was introduced.
     They're recognised by a message like fix, bug, revert, hotfix or regression, or by being
     merged from a PR linked to an issue labelled `bug`.
   - Plus explicit `review.py escape --fix <commit|PR>`.
   - Plus `review.py escape --file <path> --symbol <name> --note "…"`, for a bug with no fix yet
     or one found in another repo.
2. **Blame:** for each line the fix changed or deleted, run
   `git blame -w -M -C --ignore-revs-file .git-blame-ignore-revs` on the version just before the
   fix. That finds the commit that introduced the line, following renames and moved code and
   skipping known mass-reformat commits, and from that commit, its PR.
   - **A fix that only adds code** (a missing check) has no line to blame. The enclosing function's
     lines are blamed instead, at `confidence: low`.
3. **Classify** against that PR's history (hot, archive or expired):

   | History shows | Class | Effect |
   |---|---|---|
   | The PR was reviewed, and no finding touched those lines or that symbol | `missed` | A recall miss for the lenses that ran |
   | A finding covered it, but it was labelled `false-positive` or shipped as-is | `dismissed-real-bug` | Its outcome is corrected to a true positive. This is the most important signal: a correct finding that a human overrode. |
   | A finding covered it, labelled `accepted-risk`, and the risk came true | `accepted-materialised` | Stays a true positive; tracked as the cost of accepted risk |
   | The PR wasn't reviewed | `unreviewed` | Shows where reviews should run; doesn't count against the lenses |

4. **Confirm:** each attribution is listed for a human to confirm, re-attribute or reject. Only
   confirmed ones are written as escape records and outcome corrections.

### 4.5 Monthly retro (`review.py retro --month <yyyy-mm>`, or `--since`)

The monthly retro runs once a month, prompted by the nudge in §4.3, and on demand before any
gating decision or after a notable escaped bug. It:

1. **Rebuilds** each PR merged in the period, from its local runs, plus its posted review-core
   comments where the local files are missing (the markers carry finding ids).
2. **Reports process insights:**
   - review rounds and their efforts;
   - first-review-to-merge time;
   - findings per round;
   - which P0/P1 findings survived to merge;
   - the verdict at merge compared with the final review;
   - cost per PR and per finding;
   - which lenses fired;
   - human PR comments not from review-core (things a human flagged).
3. **Runs bug attribution (§4.4)** over the period's fix commits, presenting the attributions for
   confirmation.
4. **Lists findings still unlabelled** from earlier merges.
5. **Runs `compact`** on the closed month.
6. **Writes** a markdown summary for the human, plus `review-history/<repo>/retro/<yyyy-mm>.json`.

Everything except writing to `review-history/` is read-only: git history and `gh api` GETs. So it
works with the agent's read-only token. The agent commits the history, and the human pushes it.

## 5. Precision and the eventual gate

`review.py precision [--repo R] [--lens L] [--severity P0,P1] [--window 12m | --since-lens-change]`
prints, per lens: true positives, false positives, excluded and unlabelled counts; raw precision;
the **Wilson 95% lower bound**; the PRs and repos the findings came from; and escapes by class.
Old months are read from the aggregates, so long windows stay cheap.

An example of the gate rule it would support, not adopted here: *gate FIX-FIRST for lens L in repo
R once L has ≥ 40 labelled findings there in the last 12 months, from ≥ 10 PRs, with Wilson lower
bound ≥ 0.80, and no `dismissed-real-bug` escapes in the window.* Until a lens meets its rule,
review-core stays advisory for it.

## 6. Data-quality fixes in `report` (part of this work)

- Dedupe by `run_id`, so re-runs overwrite.
- Add `--kind test`; mark the existing posting test run, and synthetic runs found in the history,
  as `test`.
- Time `review_wall_s` from the review start, not from `prep`.
- Make `--cost` required in the procedure, with `cost_missing` when it is absent.
- Record `skill_version`, `model` and `lens_versions`.
- Bump the ledger shape to `review-core/2`, as the contract requires for shape changes, and
  document the new fields in `contract.md`. The report JSON gains `run_id` and `kind`, which are
  additive.
- **Backfill:** import the 8 existing reports (ClaudeConfig 7, faultline 1). Mark duplicates and
  test runs, then run the first retro over all 11 merged PRs. **Expect little data:** the backfill
  mainly proves the pipeline, and useful numbers build up as project repos are reviewed.

## 7. Tests proposed

All offline, using fakes for git history and GitHub, in the existing pytest style:

- **Run identity:** the same inputs give the same `run_id` and an overwrite; `--kind test` runs
  are excluded from every metric.
- **Outcome inference:** changed and absent → `fixed`; absent with no change → `unlabelled` and
  flagged; symbol deleted → `superseded`; unchanged at merge → shipped as-is, needing a label.
- **Labelling:** human labels write both the event and `suppressions.md`; the author may only
  write `disputed`; the latest event wins, and history is kept.
- **Bug attribution:**
  - The fixture: PR A introduces a bug; two months of unrelated commits follow, including a file
    rename and a reformat commit listed in the ignore-revs file; then a fix commit.
  - Blame attributes it to PR A, not to the rename or the reformat.
  - Each class is tested: `missed`, `dismissed-real-bug` (which corrects the outcome),
    `accepted-materialised` and `unreviewed`.
  - An add-only fix → the enclosing function is blamed, at low confidence.
  - Attribution to a PR older than 12 months works from the expired tier.
- **Retro:** on a fake repo with merged PRs, the process stats match hand-computed values;
  posted-comment recovery works when local runs are missing; only confirmed attributions are
  written.
- **Compaction:**
  - the archive round-trips exactly to the loose files, which are deleted only after that check;
  - the aggregates equal metrics computed from the detail;
  - expiry removes only free text, and attribution still works afterwards;
  - running it twice changes nothing.
- **Precision:** the counts and Wilson bound against hand-computed values; window and
  since-lens-change filters work; small-n output says "insufficient".
- **Backfill:** idempotent. Three copies of one run collapse to 1.
- **Concurrency:** writing events from two branches never touches the same file.

## 8. Ways the data can mislead

| # | Risk | Mitigation | Residual |
|---|---|---|---|
| 1 | **Wrong "fixed":** the code changed for an unrelated reason | Require both a code change **and** absence in the next review; a human can override | Some inflation of true positives; spot-checked in each retro |
| 2 | **Unstable ids:** a reviewer re-slugs the same defect | Match by `finding_key`, then by file, symbol and severity within the line window, the same rule as `merge` | Re-slugs in another symbol or at another severity are missed |
| 3 | **Selection bias from unlabelled findings** | Show the unlabelled count beside precision; the gate needs labelled n ≥ 40 | If labelling lapses, the gate never opens, which is the safe failure |
| 4 | **Self-review bias:** most data so far is review-core reviewing itself | Precision is per repo and per lens; ClaudeConfig data doesn't count toward a project lens | Little useful data until project repos are reviewed regularly |
| 5 | **Goodhart:** dismissing findings as `accepted-risk` | A reason is required; the retro lists them; a later `accepted-materialised` escape exposes the cost | It depends on honest labelling |
| 6 | **Author agent mislabelling** | The author can only write `disputed`, and only a human confirms | None known |
| 7 | **Test or duplicate runs leaking in** | `kind` is set at creation; `run_id` dedupes | A test run that wasn't marked; the backfill marks known ones |
| 8 | **Blame picks the wrong commit:** the last toucher is a reformat or rename, not the cause | `-w -M -C`, an ignore-revs file, and human confirmation of every attribution | Attribution is an estimate |
| 9 | **No line to blame** (an add-only fix for a missing check) | Blame the enclosing function, marked low confidence | Some misses can't be attributed |
| 10 | **Bugs never fixed or never reported** | None | Recall is undercounted |
| 11 | **Fix commits not recognised** (an unhelpful message, no issue link) | `escape --fix` for manual entry; the retro lists the period's merged PRs for the human to scan | Some escapes go unrecorded |
| 12 | **Compaction loses data** | Loose files are deleted only after the archive verifies; compaction runs on `main` only, for closed months | None known |
| 13 | **Old data skews today's numbers** after a model or lens change | Rolling 12-month window; `--since-lens-change` | Little data right after a lens change |
| 14 | **History lost before it's pushed** | Committed and pushed like any change, one file per event | Unpushed local history is lost if the machine is |

## 9. Open questions

1. **Count author `quick` rounds?** Recommendation: record them as `author-quick`, and exclude
   them from precision, because they aren't independent.
2. **Fix-commit recognition:** add a `bug` label convention for PRs and issues, so the retro
   needn't rely on commit message keywords?
3. **Gate thresholds:** n ≥ 40, ≥ 10 PRs, lower bound ≥ 0.80, and no dismissed real bugs are
   placeholders, to decide when the data exists.

## 10. Rollout

1. Review this document and settle the open questions.
2. Implement §6 (data quality) and §3 (records, hot tier), and backfill the existing reports.
3. Implement `label` and `outcomes` (§4.2–4.3), then `escape` and attribution (§4.4), then `retro`
   (§4.5), `compact` (§3.2) and `precision` (§5), each with its tests.
4. Run the first monthly retro over all merged PRs, and label and confirm what it lists.
5. Update SKILL.md, covering the procedure (`--kind`, required `--cost`), labels at merge, the
   monthly retro, and attribution; contract.md (ledger v2, outcomes, escapes); and the README.
6. Reassess gating once any lens has meaningful labelled data in a 12-month window.
