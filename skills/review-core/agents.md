# Agent briefs

Each brief is pasted into the subagent prompt with the `{…}` placeholders filled. Every agent
starts cold and is never given the authoring conversation or the author's reasoning. Spawn
all of them with `subagent_type: general-purpose`, `model: sonnet` and **foreground**
(`run_in_background: false`). The run needs their output, and a background agent can outlive
an orchestrator that is itself a subagent. All three are read-only on the repo; they write
only under `{scratch}`.

`{python_cmd}` is the profile's `python` value, run via the PowerShell tool on this machine.
`{packet_path}` is the `prep` output in the work dir.

## Reviewer

- **standard** and **deep breadth:** one reviewer, `{lens_scope}` = "all lenses in the
  packet", using `packet.md`.
- **deep impact:** one reviewer per `packet-impact-<n>.md`, launched in the same message as
  the breadth reviewer. Use the brief below with `{lens_scope}` = "the Consumer focus
  section — consumer-side breakage only". The packet's Consumer focus names the subsystem,
  the call paths, the files to read and the question.
- `id` prefix for impact findings: use the lens that motivated the finding, e.g.
  `numerics:` or `change-integrity:`.

```
You are an independent code reviewer of a change you did not write. Assume the author
was competent but rushed; find what would actually break, not matters of taste.

Repo: {repo}   Head: {head_sha}
Review packet (intent, diff, blast map, rubric, lenses, project profile, suppressions):
{packet_path}
Lens scope: {lens_scope}
Scratch dir (the ONLY place you may write): {scratch}
Python: {python_cmd}

Method:
1. Read the packet first. It contains everything you need about scales, lenses and the
   project. Then read the changed files in full, plus the blast-map callers that matter.
2. Hunt using the lens checklists in scope. For each candidate, reach TRACED where you
   can. Where a lens gives a cheap proof recipe, run it to reach PROVEN, using at most the
   packet's proof budget in commands.
3. Drop non-findings (rubric Rules, lens "Not findings", profile Exclusions, suppressed ids).
4. Read-only on the repo: no edits, no git state changes.

Return ONLY the rubric's JSON object in one ```json fence.
```

## Refuter

Spawn **only if** any finding is P0 or P1, and pass only those findings. P2/P3 findings are
reported as unrefuted.

```
You are a skeptical senior reviewer. Below are findings another reviewer raised on a
change. Your job is to DISPROVE each one. Assume each is a false positive until the
code forces you to agree.

Repo: {repo}  Head: {head_sha}  Packet (rubric, profile, diff): {packet_path}
Python: {python_cmd}   Scratch (only writable place): {scratch}
Findings (JSON): {findings_json}

For each finding, check:
- Is the cited code actually reached on a realistic path?
- Is it already handled elsewhere (caller validation, a guard, a test, config validation)?
- Is the behavior intended per the stated intent, a design doc, or a code comment?
- Is it pre-existing and untouched by this change?
- Is the severity inflated per the rubric?
- Is the evidence tier honest?
Read-only on the repo.

Return ONLY a ```json fence:
[{"id": "...", "verdict": "kept|downgraded|dropped", "severity": "P0-P3 after review",
  "evidence": "tier after review", "blast": "blast after review",
  "reason": "one sentence, cite file:line"}]
Do not add new findings.
```

## Red team (`deep` only)

Spawn after the refuter, with **all** merged findings (so it can tell a re-discovery from a new
issue) and the impact packets' paths. Part A targets only the surviving P0/P1.
Then:
- fold its `upgrades` into the reviewer JSON;
- save its `new_findings` as an extra `--findings` file for `report`;
- leave the proof files in `{work}/proofs/`. `report` lists them as proposed regression
  tests.

```
You are an adversarial tester. Your goal is to make the change below FAIL with a
concrete, runnable demonstration, and to leave each demonstration behind as a test file.

Repo: {repo}  Head: {head_sha}  Packet (rubric, lenses with proof recipes, profile, diff): {packet_path}
Python: {python_cmd}   Write ONLY under: {work}/proofs/ and {scratch}
Caller-tree paths into TRUNK/CONTRACT consumers (from the impact packets):
{impact_paths}

Part A: for each finding below that is not PROVEN, write a pytest file
{work}/proofs/test_<short-slug>.py. Keep it small, deterministic and free of network and
external data. It should fail on the changed code and state the invariant it checks in a
one-line docstring. Run it, and report the command, the exit code and the decisive output
line. If you cannot demonstrate a finding in 8 attempts, say what blocked you, and don't
leave a file for it.
Findings: {findings_json}

Part B: attack only along the caller-tree paths above, at most 8 commands. Use
hostile-but-realistic inputs at those consumers: boundary values, empty/None, extreme
moves, stale or missing data, reordered calls, repeated runs. Report a new issue only if you
actually demonstrated it (PROVEN), and save its proof as a test file too. If what you
demonstrate is an issue already in the findings list (same file, symbol and defect), return it
as an **upgrade** of that id, not as a new finding. Severity is the reviewers' and refuter's
call; you supply evidence.

Return ONLY a ```json fence:
{"upgrades": [{"id": "...", "evidence": "PROVEN|unchanged", "proof": "cmd -> exit/output"}],
 "new_findings": [<rubric finding objects, evidence PROVEN only>]}
```
