---
paths:
  - "**/*.py"
---

# Python coding standard

Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) for new
code and for code you touch, with these house deviations (they override the guide):

| Guide | House rule |
|---|---|
| Import modules only (§2.2) | `from x import Class` / `from x import func` is allowed |
| One import per line (§3.13) | ruff `I` decides import grouping |
| 80-character lines (§3.2) | the repo's configured line length |
| Use a formatter | in ruff-only repos (faultline, MacroRAG; XbrlRag after its phase B), no formatter: ruff lint only, never black, isort or `ruff format`. Elsewhere, use the repo's own formatter |
| Avoid abbreviations (§3.16) | standard import aliases are fine (`np`, `pd`, `ql` for QuantLib) |
| Naming (§3.16) | names dictated by an external API keep that API's form |

A repo's own formatter hook or `CLAUDE.md` overrides the formatting row above.

Use the guide's own allowances as written: a catch-all `except` at an isolation point (§2.4), and
short names that follow established mathematical notation, with a reference (§3.16.4).

**Scope:**
- New files, functions and classes follow the guide in full.
- When you refactor a function or class, bring the whole unit to the guide: signature types,
  docstring, names, exception handling.
- For a small edit inside a function, only the changed lines follow the guide.
- Don't restyle untouched code, including neighbours in the same file. Bulk restyling happens only
  in dedicated ratchet changes.

**Rules no tool checks:**
- Never weaken lint or type config to pass a check: no new ignores, `ruff-burndown.toml` entries,
  mypy overrides, removed rules or loosened version pins. Fix the code, or stop and report.
- Type hints on new and changed functions.
- Docstrings in Google format (`Args:`, `Returns:`, `Raises:`) on new public functions, classes
  and modules. Don't repeat the type hints in prose.
- A `# noqa` or `# type: ignore` names the rule or error code and says why.
- Randomness takes a seed or a `Generator` as a parameter; no module-level random state.
- New dependencies and tools are added with an exact pinned version.

Testing rules: the `tdd-spec` skill (design §8). Architecture conventions (fail-loud, typed config,
pydantic at boundaries): the global `CLAUDE.md`.
