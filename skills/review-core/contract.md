# Review contract (review-core/2)

The one standard every reviewer, refuter and red-team agent reports against. If a lens
file and this contract disagree, this contract wins; a project `profile.md` may only *add*
contract surfaces, exclusions and proof recipes — it never redefines the scales.
[rubric.md](rubric.md) is the condensed copy injected into agent packets; keep the two in
sync (a test checks the scale names).

## 1. Severity — what happens if this ships

| Sev | Name | Anchor (any one is enough) |
|---|---|---|
| **P0** | Blocker | Wrong numbers/results reach an output or consumer **silently**; persisted data corrupted or lost; exploitable security hole with a credible path; secret/real identifier committed; provenance broken so a run is no longer auditable. |
| **P1** | Major | Incorrect behavior on a realistic path (but detectable or bounded); a fail-loud contract now swallows or substitutes; a contract surface changed without migration/lockstep; tests weakened to pass; unexplained golden/baseline drift. |
| **P2** | Minor | Bug on a rare/edge path; changed behavior on a non-critical path with no test; measurable perf or cost regression; misleading doc/comment about changed code. |
| **P3** | Nit | Clarity/cleanup a senior engineer would still mention. Capped at 5 in the report; the rest collapse to a count. Never raised at INFERRED. |

Severity is about **consequence × likelihood of the defect**. It is not inflated because
the file is important — that is what blast radius (§3) records.

## 2. Evidence — how we know (replaces "confidence %")

Stated percentages from a model are not calibrated, so none are reported. Each finding
carries the strongest evidence actually obtained:

| Tier | Meaning | Required artifact in `proof` |
|---|---|---|
| **PROVEN** | A command was run and demonstrated the failure (failing test, repro script, runner exit code, numeric invariant violated). | The exact command + its exit code / key output line. |
| **TRACED** | The failing path was followed end to end in the code: input → changed line → wrong effect. | A `file:line → file:line → …` chain. |
| **INFERRED** | Pattern match / reasoning without a full trace. | One sentence saying what was *not* verified. |

PROVEN means the command demonstrated the **failure** itself. A command that only confirms
a fact about the code (a grep, an AST scan, a passing test) supports a TRACED finding.

Rules: INFERRED P3 is dropped. The refuter runs **only when there are P0/P1 findings**
and attacks only those; P2/P3 are reported unrefuted (the ledger says so). At `deep`
effort the red-team pass must attempt to upgrade INFERRED P0/P1 to PROVEN (or it is
downgraded one level with `proof` explaining why it could not be shown).

Real calibration comes from the ledger (§6), not from self-rating.

## 3. Blast radius — how far it reaches

| Level | Meaning |
|---|---|
| **LOCAL** | Private to the changed function/class; no other caller affected. |
| **MODULE** | Callers inside the same package/subsystem. |
| **CROSS** | Imported/consumed by another subsystem. |
| **CONTRACT** | Crosses a durable boundary: persisted data or schema, public API/service, config schema, env-var contract, provenance/snapshot stamps, golden baselines, an external service or UI consumer. The project `profile.md` lists these surfaces. |

Blast radius is **measured, not guessed**. `scripts/review.py prep` builds a blast map before
review:
- For every changed symbol, private ones included, it follows the **transitive caller tree**:
  up to 3 hops, through enclosing functions and wrapper methods.
- That tree gives a computed reach level and the **path that justifies it**, e.g.
  `var._var_es ← VarEngine.historical_var ← service.risk_var [route]`.
- Exposure roots are route handlers, CLI `main`, and contract-surface files.
- Test callers are counted, not treated as reach. Names defined in several places are
  flagged `ambiguous`.

A finding cites the map entry it relied on.

**Tier** is separate from blast. TRUNK / BRANCH / LEAF says *where* the change lands; it comes
from the pack's committed `codemap.json`, built from the profile's `criticality` declaration.
It is shown beside blast and **never changes severity**. A TRUNK file deserves closer
reading, and the report suggests `deep` effort.

## 4. Finding schema

Each reviewer returns one JSON object in a ```json fence (validated by
`scripts/review.py report`); `lens` names the lens that motivated each finding:

```json
{
  "findings": [{
    "id": "<lens>:<path>:<symbol>:<kebab-slug>",
    "severity": "P0|P1|P2|P3",
    "evidence": "PROVEN|TRACED|INFERRED",
    "blast": "LOCAL|MODULE|CROSS|CONTRACT",
    "lens": "<lens name>",
    "file": "repo/relative/path.py",
    "line": 123,
    "symbol": "function_or_class",
    "summary": "One sentence: the defect.",
    "failure": "One sentence: concrete input/state -> wrong outcome, and who is affected.",
    "fix": "One sentence: the fix, and what it must keep working.",
    "proof": "command+exit | file:line chain | what was not verified"
  }],
  "checked": ["one line per thing examined, e.g. 'callers of size_book (3 files)'", "..."],
  "not_checked": ["what this lens could not examine and why"]
}
```

`id` is a stable fingerprint (no line number) so suppressions survive code moves.

## 5. Human report template (terminal)

Concise by rule: ≤ 2 sentences per field, no praise section, no restating the diff,
no findings about lines the change did not touch unless the change made them newly
reachable or worse (then say "pre-existing, now reachable").

```
## Review · <target> @ <short-sha> · <effort> · verdict: <VERDICT> (advisory)
Intent: <one line — what the change claims to do>

| # | Sev | Evidence | Blast | Where | Finding |
|---|-----|----------|-------|-------|---------|
| 1 | P1 | TRACED | CONTRACT | [x.py:88](x.py#L88) | <summary> |

**1. <summary>**  Failure: <failure> Fix: <fix>  Proof: <proof>
...
<n> P3 nits collapsed (see JSON).

### Validation ledger
- Lenses: change-integrity, numerics (reviewers: 1, fresh context: yes)
- Read: <n> changed files, <m> dependents (blast map: <k> importers, contract surfaces: <list|none>)
- Executed: `<cmd>` → exit 0; `<cmd>` → exit 1 (<why>)
- Skipped: <what + why, e.g. governance_runner — data/marketdata absent>
- Refuter: <in> in → <kept> kept, <down> downgraded, <dropped> dropped | skipped (no P0–P1) · Suppressed: <s>
- Report: <path to JSON>
```

`scripts/review.py report` produces this from the JSON report — don't hand-write it. It also
enforces two rules:
- **Locations.** Every `file:line` is checked against the file and the cited symbol's span. A
  wrong line is relocated to the first changed line inside the symbol, or its start, with a
  warning; the original is kept as `line_reported`. When reviewers raise the same finding,
  the uncorrected citation wins on an evidence tie.
- **Merging.** Findings are the same when their ids match, ignoring the lens prefix. Two
  *different* reviewers' findings with different ids also merge when they share the file,
  a non-empty symbol and the severity, and cite lines within 5 of each other. One
  reviewer's distinct ids are never merged.
- **Refuter.** A refuter can only cut. `downgraded` may lower severity, evidence and blast.
  `kept` may only weaken evidence or narrow blast, and it leaves PROVEN alone. A value that
  would raise anything is ignored. A "downgrade" that lowers nothing counts as kept. A verdict
  other than kept, downgraded or dropped is warned about, and the finding stays unrefuted.
- **Brevity.** The terminal report clips the summary to 1 sentence, failure and fix to 2,
  and the proof by length. The JSON keeps the full text, and the `Skipped` list is
  de-duplicated and capped at 5.

Verdict: **BLOCK** (any P0) · **FIX-FIRST** (any P1) · **SHIP-WITH-NOTES** (P2/P3 only) ·
**SHIP** (none). With zero findings the ledger is still printed — "no findings" without
coverage is not a result.

## 6. Machine report + calibration

Written by `scripts/review.py report` to `<repo>/.claude/review/out/` (gitignored — the
one write into the reviewed repo the skill is allowed; PR reviews write to the user's
checkout, not the temp worktree). `--out-dir` overrides it:

- `<UTC yyyymmddThhmm>-<short-sha>.json` and a copy at `latest.json`:
  `{schema:"review-core/2", repo, target, head_sha, effort, mode:"advisory", verdict,
  intent, kind, run_id, ledger:{lenses, reviewers, gate, blast_map, checked, skipped,
  refuter:{in,kept,downgraded,dropped,ran}, suppressed, deep, impact, diff_tests, proofs,
  tokens, wall_s}, findings:[…], warnings:[…], report_path}`.
  - `repo` is the origin repo's name, for PR reviews too. `kind` is one of
    `independent`, `author-quick` or `test`. `run_id` identifies the run in the history, and
    re-running `report` on the same inputs keeps it.
  - `wall_s` is the review time, from `review.py start` to `report`, or null; it was formerly
    measured from `prep`.
  - review-core/2 differs from /1 only by these additive fields and the `repo` and `wall_s`
    meanings.
  - `gate` is the list of gate results (`cmd`, `exit`, `tail`, or `skipped`); executed
    commands are the entries with a non-null `exit`.
  - Each finding is a §4 object plus `refuter: kept|downgraded|unrefuted`, and, when they
    apply, `raised_by` (count of reviewers that raised it) and `line_reported` (the cited
    line before relocation). `code_fp` is a hash of the cited code as reviewed: the
    symbol's lines, else the cited line ± 2.
- `review.py status` reads `latest.json` (or `--report`) and prints its findings as a
  table. It compares each finding's `code_fp` with the code now, and marks it open, changed
  since review (recheck), unknown (this checkout doesn't contain the reviewed commit) or
  suppressed.
- PR reviews read `suppressions.md` from the user's checkout, never from the PR, so a
  re-run of `report` after the worktree is gone gives the same result.
- `ledger.jsonl` — one line per run: `{ts, target, head_sha, effort, verdict,
  counts:{P0..P3}, evidence:{PROVEN,TRACED,INFERRED}, refuter:{in,kept,downgraded,dropped},
  per_lens:{name:count}, tokens:{agent:n,…,total}, wall_s}`. `tokens` comes from
  `report --cost`, and makes "what does a review cost per MR?" answerable.
- **Posted form (GitHub, `report --post`):**
  - One summary comment, marked `<!-- review-core:summary -->` and edited in place. Its first
    line carries the verdict icon, the TRUNK touch, the reach and the effort; the ledger sits
    in a `<details>` block with no local paths.
  - P0/P1 findings on diff lines become inline comments marked
    `<!-- review-core:id=<id> -->`, so they can be matched on re-review: a finding now gone is
    replied to once and its thread resolved; one reported again has its thread reopened.
    Resolving and reopening need repository write access. Without it, the reply is still
    posted, and the report names the threads to resolve by hand.
  - Labels: `ai-verdict:*` and `review:trunk|contract|deep`.
  - The review is always posted with the `COMMENT` event. The AI never approves.

Suppressions live in the committed `<repo>/.claude/review/suppressions.md`:
`- <id> — false-positive|accepted-risk|wontfix — <reason> — <yyyy-mm-dd>`.
Matching ids are not re-raised (counted in the ledger).

**Outcomes and precision.** The history (ClaudeConfig `review-history/<repo>/`; design in
[design/outcomes-and-retro.md](design/outcomes-and-retro.md)) gives every finding an outcome.

| Outcome | Precision counts it as |
|---|---|
| `fixed`, `accepted-risk`, `wontfix`, `dismissed-real-bug`, `accepted-materialised` | **True positive.** The defect was real, even if not acted on. |
| `false-positive` | **False positive** |
| `superseded`, `disputed` (until a human confirms it), `unlabelled` | Excluded, but counted and shown |

- **Only a human labels a finding.** The author agent may only mark one `disputed`.
- **Per-lens precision** = TP ÷ (TP + FP). It counts only `independent` runs, in a rolling
  12-month window (or since the lens last changed). It is reported with its sample size and
  the Wilson 95% lower bound, the number that decides when a lens is trustworthy enough to
  gate.
- **Escapes**, bugs attributed to the PR that introduced them, are classed as `missed`,
  `dismissed-real-bug`, `accepted-materialised` or `unreviewed`. They are the recall signal.

## 7. Gating (future — not active in advisory mode)

A later hook may read `latest.json` and block on `verdict == BLOCK`, or on FIX-FIRST for
lenses whose measured precision ≥ an agreed threshold. The JSON shape above is that
interface; don't change it without bumping `schema`.
