# Security policy

ClaudeConfig is a personal project maintained on a best-effort basis.

## Reporting a vulnerability

Please report a vulnerability privately through GitHub's **Report a vulnerability** button on the
repository's **Security** tab (private vulnerability reporting), not in a public issue. Include the
affected file, steps to reproduce, and the impact you expect.

There is no fixed response time. Only the latest commit on `main` gets fixes.

## Scope notes

- **Adopting this config runs its code.** `settings.example.json` wires hooks and a status line
  that execute Python on your machine, and skills tell an agent what to run. Read them first.
- **`skills/tdd-spec/install.ps1` needs an elevated shell** and installs hooks under
  `C:\Program Files\ClaudeCode`. Run it only after reviewing the committed scripts, from a session
  no agent has touched. [SKILL.md](skills/tdd-spec/SKILL.md) and the threat model in
  [design/plan.md](skills/tdd-spec/design/plan.md) explain why, and what it does not protect.
- **The tdd-spec guards are a process control, not a sandbox.** On a user-writable toolchain they
  stop mistakes, not a determined local process.
- **`policy/` describes one setup** that keeps agents read-only on GitHub. The check script probes
  your own token; it does not make any setup safe by itself.
- **Local records stay local.** `review-history/`, `tdd-history/` and the cost ledger can hold file
  paths and review text from your private code. `.gitignore` excludes them; keep it that way unless
  the repo is private.
- Never put real credentials in an issue, pull request or commit.
