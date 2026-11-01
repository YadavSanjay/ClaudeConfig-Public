# Design: agents can't change GitHub; only the human pushes and deletes

Status: **implemented and verified (2026-10-03).** This replaces an earlier proposal for a local
command guard (PowerShell PreToolUse hook, managed settings, global pre-push hook). §7 explains
why that proposal was dropped.

## 1. Problem

AI agents on this machine (Claude Code sessions and their subagents) used the user's own GitHub
credentials: git's stored login and the `gh` CLI token, both with full `repo` scope. With them, an
agent could:
- push anything;
- delete remote branches and tags (`git push --delete`, `gh pr merge --delete-branch`, the API);
- change repository settings, such as auto-deleting merged branches.

The only control was an instruction in `CLAUDE.md`, and the user-level `settings.json` even
auto-approved `git push *`. Deleting a branch, tag or repository is hard to undo.

**Goal:** an agent on this machine can't push, delete or change settings on GitHub, by any route:
a command, a script, a helper task or a settings file. The human keeps full control.

**Constraint:** a free GitHub account. Rulesets and branch protection on private repositories need
a paid plan, so the server can't be asked to block deletions for "write but not delete".

## 2. Principle: enforce with GitHub permissions, not with local rules

GitHub decides what a token may do, and that decision can't be bypassed from this machine, so it
holds however an agent reaches GitHub. Local rules (instructions, command filters) can be
sidestepped. A script can call `gh` itself, a settings file can add commands, and a browser tool
can click a button. Local measures here therefore only explain refusals and reduce noise; they are
not the control.

## 3. The model

| Who | Credential | Can do on GitHub |
|---|---|---|
| **Agent** | A fine-grained personal access token used by `gh`, and by git over HTTPS through `gh auth setup-git` | **Read** code; open PRs; post PR comments, reviews and labels; edit PR titles. **Cannot** push, delete branches or tags, merge, resolve review threads or change repo settings. |
| **Human** | An SSH key with a passphrase, typed on every push (never cached in `ssh-agent`), plus the github.com website | Everything: push, merge, delete branches and tags, settings |

**The token** (github.com → Settings → Developer settings → Fine-grained tokens):
- **Repositories:** only the ones listed in `REPOS` in `policy/local.env`.
- **Permissions:** Contents read-only, Pull requests read and write, Metadata read; everything else
  no access.
- **Expiry:** 90 days.

**The push path:** every repo has `remote.origin.pushurl = git@github.com:<owner>/<repo>.git`,
while fetch stays on HTTPS. Pushes use `~/.ssh/id_ed25519_github` (passphrase-protected,
`IdentitiesOnly yes` in `~/.ssh/config`). The Windows `ssh-agent` service is disabled, so the key
is never unlocked for other processes.

**The workflow:**
1. The agent commits locally.
2. The human pushes, from VS Code or a terminal, typing the passphrase.
3. The agent may open the PR (`gh pr create --head <branch>`), review it and post.
4. The human merges, and deletes branches, on github.com.

## 4. What was removed from the agent's reach

| Credential or route | Action taken |
|---|---|
| `gh` OAuth token (`repo` scope) | Replaced by the fine-grained token |
| Git Credential Manager token (`git:https://github.com`, `repo` scope) | Deleted. `~/.gitconfig` resets the helper list for github.com and uses only `gh`, so git never asks the credential manager. |
| A stray fine-grained token found in the credential manager | Deleted. It was most likely stored by a git prompt during setup. |
| GitHub Desktop login (`GitHub - https://api.github.com/<user>`) | Removed |
| VS Code passing its GitHub login to integrated terminals | `git.terminalAuthentication: false` |
| The user's logged-in Chrome through Claude in Chrome | Not used (user decision) |
| Cloud agents through a Claude GitHub App | No such app installed |

## 5. Verification

Run [verify-github-access.sh](verify-github-access.sh) after rotating the token, changing git or
VS Code settings, or upgrading tools. It is non-destructive: write access is probed with
`git push --dry-run`, which GitHub refuses before anything is sent, and with a repo `PATCH` that
re-sends the repo's own name.

It checks:
- that the only stored GitHub credentials are `gh`'s;
- that the `gh` token is fine-grained;
- that the agent shell has no askpass or token variables;
- that the git credential manager has nothing for github.com;
- that the SSH key is encrypted and `ssh-agent` is disabled;
- that VS Code's `git.terminalAuthentication` is false;
- per repo: read works, push is denied (403), the push URL is SSH, and settings changes are
  denied (403).

**Result on 2026-10-03: all checks pass.**

These were also run by hand on 2026-10-03, each against a branch name that doesn't exist:
- `git push --delete` over HTTPS → 403 "Write access to repository not granted";
- `gh api -X DELETE …/git/refs/heads/…` → 403 "Resource not accessible by personal access token";
- `gh repo edit --delete-branch-on-merge` → 403;
- `git push` through the SSH push URL from the agent → "Permission denied (publickey)".

**review-core posting** was tested on a throwaway PR. It opened the PR; created and edited
the summary comment; posted the inline comment; created, added and swapped labels; replied to the
thread; and edited the title. All of that works with this token. **Resolving a review thread is
refused (FORBIDDEN):** it needs repository write access. review-core reports which threads the
human should resolve.

## 6. Remaining risks

| # | Route | Why it remains | Exposure |
|---|---|---|---|
| 1 | **VS Code's GitHub session token**, stored encrypted with the user's Windows login | Any process running as the user can decrypt it. VS Code doesn't pass it to agent shells, so only an agent that *deliberately* extracts it can use it. | Low: needs deliberate credential theft. Closing it fully means signing VS Code out of GitHub (Copilot needs the sign-in), or running agents under a separate Windows account. |
| 2 | **The human typing the passphrase for an agent-started push** | If an agent's `git push` ever prompts in a UI, the human might type the passphrase. `git.terminalAuthentication: false` removes the VS Code terminal prompt. | Low: only push a branch you started pushing. |
| 3 | **Browser and computer-use tools** clicking on github.com | Claude in Chrome isn't used. The built-in browser isn't signed in to GitHub. Computer-use could drive the user's own browser. | Low: keep computer-use off for GitHub work. |
| 4 | **The token expires or is re-created with more permissions** | Human error during rotation | Re-run `verify-github-access.sh` after every rotation. |
| 5 | **A new repo is added to the token** | It gets the same read-only permissions, which is safe. Its push URL must still be set to SSH. | Add the repo to `REPOS` in the script, and run it. |
| 6 | **Another machine or a cloud session** | This setup is per machine. | Repeat the setup there; there are no cloud agents today. |

## 7. Why not the local command guard

The first proposal relied on a PowerShell PreToolUse hook installed through managed settings, plus
a global pre-push hook. A critical review showed it couldn't meet the goal:
- **Scripts:** a helper script that calls `gh` deletes a branch without any command the guard can
  see.
- **Repo settings:** a merge can delete the head branch if an agent first turns on auto-delete,
  which the old token allowed.
- **Settings files:** these can add commands that never pass through the guard (SessionStart,
  statusLine, `env` overriding git config).
- **The git hook:** it depended on user-writable git config. It was also disabled by any repo-local
  `core.hooksPath` (husky), and it silently turned off every repo's own hooks.
- **Credentials:** the git credential manager kept its own full token, out of reach of any `gh`
  changes.
- **Pattern matching** can never list every spelling of a command.

Server-side rulesets would have fixed this, but they need a paid plan for private repos. Taking
write access away from the agent's credential achieves the goal on a free account. It costs one
manual push per branch, and the human resolves review threads.

## 8. Operations

- **Rotating the token** (every 90 days):
  1. Create a new token with the same settings.
  2. `gh auth login --hostname github.com --git-protocol https` and paste it, in your own
     terminal, not through an agent.
  3. Revoke the old token.
  4. Run the script.
- **New repo:** add it to the token's repository list. Set its SSH push URL with
  `git remote set-url --push origin git@github.com:<owner>/<repo>.git`. Add it to `REPOS` and
  run the script.
- **New machine:** repeat §3 and §4, then run the script.
- **If the stray credential manager entry ever reappears**
  (`cmdkey /list | Select-String "git:https://github.com"`): delete it, and check that
  `~/.gitconfig` still has the `credential.https://github.com.helper` lines from
  `gh auth setup-git`.
