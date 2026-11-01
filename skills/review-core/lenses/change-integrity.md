---
name: change-integrity
applies_to: ["**"]
---
# Change integrity

This lens covers only what the built-in `/code-review` does not. That command already hunts
generic logic, boundary and None bugs, resource leaks and swallowed exceptions, and
`/simplify` covers reuse. Don't duplicate them here. These items need the blast map, the stated
intent, or the assumption that the diff may be agent-written.

## Hunt for
- **Caller impact** — for each blast-map entry, does a changed signature, default, return
  shape, unit, or raised exception break a caller that wasn't updated?
- **Lockstep misses** — the change moves one of N places that must move together (code +
  config + fixture, writer + reader, enum + mapping, two surfaces of one catalog,
  baseline + its pinned date).
- **Weakened verification** — deleted/relaxed asserts, loosened tolerances, new
  `skip`/`xfail`, broadened `except` in tests, golden/baseline values edited to match new
  output, tests changed in the same diff as the behavior they check without explanation.
- **Tautological tests** — asserting on the mock's own return value, or testing the fake
  instead of the code.
- **Intent conformance** — changes the stated intent doesn't cover (drive-by refactors,
  renamed public symbols), and silent changes to a default, threshold, clamp, budget,
  timeout or retry count.
- **Hallucinated APIs on runtime-only paths** — calls, kwargs, attributes, config keys or
  CLI flags that don't exist and that the type-checker can't see (dynamic access, untyped
  libs, config dicts).
- **Doc / CLAUDE.md claim drift** — docstrings, CLAUDE.md, design docs or test comments that
  claim behavior or coverage the code doesn't have.

## Severity anchors
- **P1**: a test or baseline weakened in a way that hides a real behavior change; a caller
  broken on a live path; a lockstep miss on a contract surface; a hallucinated API on a live
  path; a silent threshold change on a risk, limit or budget.
- **P2**: scope creep with CROSS/CONTRACT blast; a doc claiming false behavior of production
  code.
- **P3**: a test or doc comment overstating coverage that is complete today. Future drift is
  hypothetical, and the blast is where the inaccurate text lives, usually LOCAL.

## Proof recipes
- `git diff <range> -- tests/ | grep -nE '^-\s*assert|xfail|skip|rel_tol|abs_tol|approx'`
- Import-check a suspicious symbol in the project env:
  `python -c "import m; print(hasattr(m, 'x'))"`.
- Run the scoped tests for the changed module (profile `tests_map`) at the working tree.

## Not findings
- Mechanical renames that the intent announces.
- Test updates that follow an intended, documented behavior change.
- Generic bugs with no link to callers, intent or tests (leave them to `/code-review`).
