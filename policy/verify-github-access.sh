#!/usr/bin/env bash
# Verify the agent's GitHub access is read-only (see DESIGN.md). Non-destructive: write access is
# probed with `git push --dry-run` (GitHub checks permission before anything is sent) and a repo
# PATCH that re-sends a setting's current value. Run after rotating the token, changing git or
# VS Code settings, or upgrading tools:
#
#   bash policy/verify-github-access.sh
#
# Exit 0 = every check passed; 1 = at least one FAIL (printed).
set -u
# OWNER, REPOS, ROOT and GH come from policy/local.env; copy local.env.example to start one.
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/local.env" ] && . "$HERE/local.env"
: "${OWNER:?set OWNER in policy/local.env}" "${REPOS:?set REPOS in policy/local.env}" "${ROOT:?set ROOT in policy/local.env}"
GH=${GH:-gh}
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
fails=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; fails=$((fails + 1)); }

echo "== stored GitHub logins (only gh:github.com... may exist)"
stored=$(cmdkey //list 2>/dev/null | grep -i 'target=' | grep -i github | sed 's/.*target=//')
others=$(printf '%s\n' "$stored" | grep -v '^gh:github.com' | grep -v '^$')
[ -z "$others" ] && ok "no other GitHub credentials stored" || fail "other GitHub credentials stored: $others"

echo "== gh token"
status=$("$GH" auth status 2>&1)
if printf '%s' "$status" | grep -q 'Token: github_pat_'; then ok "fine-grained token"
else fail "gh is not logged in with a fine-grained token (github_pat_)"; fi

echo "== agent shell environment"
leak=$(env | grep -iE '^(GIT_ASKPASS|SSH_ASKPASS|VSCODE_GIT_ASKPASS[A-Z_]*|GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN)=' | cut -d= -f1)
[ -z "$leak" ] && ok "no credential helpers or tokens in env" || fail "credential env present: $leak"

echo "== git credential manager has nothing for github.com"
if printf 'protocol=https\nhost=github.com\n\n' | git credential-manager-core get >/dev/null 2>&1; then
  fail "git credential manager returned a github.com credential"
else ok "none"; fi

echo "== SSH push key"
key=~/.ssh/id_ed25519_github
# Read the cipher name from the OpenSSH key header ("none" = no passphrase). `ssh-keygen -y -P ""`
# is not reliable here: without a terminal it exits 0 and prints nothing.
cipher=$(sed '1d;$d' "$key" 2>/dev/null | tr -d '\r\n' | base64 -d 2>/dev/null | head -c 40 | tr -c '[:alnum:]-' ' ' | awk '{print $2}')
if [ ! -f "$key" ]; then fail "missing $key"
elif [ "$cipher" = "none" ] || [ -z "$cipher" ]; then fail "$key has no passphrase (cipher: ${cipher:-unreadable})"
else ok "passphrase-protected ($cipher)"; fi
agent=$(powershell.exe -NoProfile -Command "'{0} {1}' -f (Get-Service ssh-agent).Status,(Get-Service ssh-agent).StartType" 2>/dev/null | tr -d '\r')
[ "$agent" = "Stopped Disabled" ] && ok "ssh-agent service stopped and disabled" || fail "ssh-agent service: $agent"

echo "== VS Code"
vs="$APPDATA/Code/User/settings.json"
grep -qE '"git\.terminalAuthentication"\s*:\s*false' "$vs" 2>/dev/null \
  && ok "git.terminalAuthentication is false" || fail "git.terminalAuthentication is not false in $vs"

for r in $REPOS; do
  echo "== $r"
  dir=$ROOT/$r; url=https://github.com/$OWNER/$r.git
  [ -d "$dir/.git" ] || { fail "no checkout at $dir"; continue; }
  git -C "$dir" ls-remote --heads "$url" >/dev/null 2>&1 && ok "read" || fail "cannot read"
  out=$(git -C "$dir" push --dry-run "$url" HEAD:refs/heads/agent-write-probe 2>&1)
  printf '%s' "$out" | grep -q 'returned error: 403' && ok "push denied (403)" || fail "push NOT denied: $(printf '%s' "$out" | head -1)"
  pushurl=$(git -C "$dir" remote get-url --push origin 2>/dev/null)
  case "$pushurl" in git@github.com:*) ok "push URL is SSH" ;; *) fail "push URL is not SSH: $pushurl" ;; esac
  out=$("$GH" api -X PATCH "repos/$OWNER/$r" -f name="$r" 2>&1)   # re-sends its own name: a no-op if allowed
  printf '%s' "$out" | grep -q 'HTTP 403' && ok "repo settings denied (403)" || fail "repo settings NOT denied"
done

echo
[ "$fails" -eq 0 ] && echo "PASS: agent GitHub access is read-only" || echo "FAIL: $fails check(s) failed"
[ "$fails" -eq 0 ]
