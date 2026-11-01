<#
.SYNOPSIS
  Install, update or remove the tdd-spec managed hooks. Run from an ELEVATED PowerShell.

.DESCRIPTION
  Into <Target> (admin-only):
    tdd\           the hook scripts (scripts\*.py)
    tdd\python\    a private, stdlib-only interpreter that runs the hooks, copied from -Python
    managed-settings.d\tdd.json   the hook commands, with the event log folder, git.exe and the
                                  test interpreter written in (never read from the environment)
  Tests still run on -Python (the project's interpreter). Never run VS Code or `claude` elevated:
  that would give the agent admin rights. Design: design\plan.md, section 10.1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param(
    # The project's interpreter: runs the spec tests, and is the source of the private runtime.
    [string]$Python = "",
    [string]$Target = "C:\Program Files\ClaudeCode",
    [string]$HistoryDir = $(if ($env:CLAUDE_CONFIG_DIR) { Join-Path $env:CLAUDE_CONFIG_DIR "tdd-history" } else { Join-Path $HOME ".claude\tdd-history" }),
    [string]$Git = $(try { (Get-Command git -ErrorAction Stop).Source } catch { "" }),
    # Optional: lets the gate recognise a merged impl/<id> PR whose branch was deleted (via GitHub).
    [string]$Gh = $(try { (Get-Command gh -ErrorAction Stop).Source } catch { "" }),
    # Running elevated means trusting every input. Required when a source non-admins can modify
    # (the Python env, git, this skill's scripts) would be executed or copied into the install.
    [switch]$AcceptWritableSource,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$skill = $PSScriptRoot
$tdd = Join-Path $Target "tdd"
$runtime = Join-Path $tdd "python"
$dropinDir = Join-Path $Target "managed-settings.d"
$dropin = Join-Path $dropinDir "tdd.json"

if ($Uninstall) {
    if (Test-Path $dropin) { Remove-Item -Force $dropin }
    if (Test-Path $tdd) { Remove-Item -Recurse -Force $tdd }
    Write-Output "Removed $dropin and $tdd. Restart Claude Code sessions."
    return
}

if (-not $Python -or -not (Test-Path $Python)) { throw "Pass -Python <project python.exe>; none found at '$Python'." }
if (-not $Git -or -not (Test-Path $Git)) { throw "git.exe not found (pass -Git <path to git.exe>)." }

function Get-NonAdminWriters([string]$Path) {
    $out = @()
    foreach ($rule in (Get-Acl -Path $Path).Access) {
        $who = $rule.IdentityReference.Value
        $rights = $rule.FileSystemRights.ToString()
        if ($rule.AccessControlType -eq "Allow" -and $who -match "Users|Everyone|Authenticated|$([regex]::Escape($env:USERNAME))" -and
            $rights -match "Write|Modify|FullControl|CreateFiles|AppendData") {
            $out += "${Path}: $who has $rights"
        }
    }
    return $out
}

# --- Preflight, PowerShell only: nothing has been executed or copied yet.
$prefix = Split-Path -Parent $Python
$sources = @()
foreach ($p in @($prefix, $Python, (Join-Path $prefix "Lib"), $Git, (Join-Path $skill "scripts"))) { $sources += Get-NonAdminWriters $p }
if ($sources.Count -gt 0) {
    Write-Output "These install sources can be modified by non-admin users (including an agent session):"
    $sources | ForEach-Object { Write-Output "  $_" }
    if (-not $AcceptWritableSource) {
        Write-Output ""
        Write-Output "Nothing was installed. Installing runs these elevated and copies them into the admin-only"
        Write-Output "install, so it trusts whatever they contain now. If you accept that (for example, no agent"
        Write-Output "session has run since you last reviewed them), rerun with -AcceptWritableSource."
        exit 1
    }
    Write-Output "Accepted with -AcceptWritableSource."
}

# Only committed, reviewed scripts are installed.
$repo = Split-Path -Parent (Split-Path -Parent $skill)
$dirty = & $Git -C $repo status --porcelain -- "skills/tdd-spec/scripts"
if ($LASTEXITCODE -ne 0) { throw "git status failed" }
if ($dirty) {
    Write-Output "Uncommitted changes in skills\tdd-spec\scripts; install only committed, reviewed code:"
    $dirty | ForEach-Object { Write-Output "  $_" }
    exit 1
}
$commit = & $Git -C $repo log -1 "--format=%h %s (%an, %ad)" "--date=short" -- "skills/tdd-spec/scripts"
Write-Output "Installing scripts as of commit: $commit"

foreach ($d in @($tdd, $dropinDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
# Fresh copy: remove whatever a previous version installed.
Get-ChildItem -Path $tdd -Force | Remove-Item -Recurse -Force
Copy-Item -Path "$skill\scripts\*.py" -Destination $tdd

# The private runtime, copied with PowerShell (py312 itself is not executed elevated): python.exe,
# its DLLs and the standard library without site-packages, tests or GUI modules.
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
Get-ChildItem -Path $prefix -File | Where-Object { $_.Extension -eq ".dll" -or $_.Name -in @("python.exe", "pythonw.exe") } |
    Copy-Item -Destination $runtime
if (Test-Path (Join-Path $prefix "DLLs")) { Copy-Item -Path (Join-Path $prefix "DLLs") -Destination $runtime -Recurse }
$skipLib = @("site-packages", "test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3")
New-Item -ItemType Directory -Force -Path (Join-Path $runtime "Lib") | Out-Null
Get-ChildItem -Path (Join-Path $prefix "Lib") | Where-Object { $_.Name -notin $skipLib } |
    Copy-Item -Destination (Join-Path $runtime "Lib") -Recurse
$hookPython = Join-Path $runtime "python.exe"
$probe = & $hookPython -I -S -c "import ast, json, tomllib, subprocess, hashlib, tempfile, argparse, tokenize; print('ok')"
if ($probe -ne "ok") { throw "the private runtime does not start" }

& $hookPython -I -S (Join-Path $tdd "hooks.py") settings --python $hookPython --install-dir $tdd `
    --history-dir $HistoryDir --git $Git --test-python $Python @(if ($Gh) { "--gh", $Gh }) --out $dropin
if ($LASTEXITCODE -ne 0) { throw "hooks.py settings failed" }

$version = ((Select-String -Path (Join-Path $tdd "tddlib.py") -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value)
Write-Output "Installed tdd-spec $version hooks:"
Write-Output "  settings: $dropin"
Write-Output "  scripts:  $tdd"
Write-Output "  runtime:  $hookPython"
Write-Output "  events:   $HistoryDir"
Write-Output "  git:      $Git"
Write-Output "  gh:       $(if ($Gh) { $Gh } else { "(not found: merges are recognised by subject or branch tip only)" })"
Write-Output "  tests on: $Python"
Write-Output ""

Write-Output "Installed script hashes (SHA256), to compare with the reviewed commit if in doubt:"
Get-ChildItem -Path $tdd -Filter *.py | ForEach-Object {
    Write-Output ("  {0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name)
}
Write-Output ""

Write-Output "Trust boundary check (design\plan.md 10.1):"
$bad = @()
foreach ($p in @($Target, $tdd, $runtime, (Join-Path $runtime "Lib"), $hookPython, $dropinDir, $dropin)) {
    $bad += Get-NonAdminWriters $p
}
if ($bad.Count -gt 0) {
    Write-Output "FAILED: non-admin principals can write to the managed install:"
    $bad | ForEach-Object { Write-Output "  $_" }
    exit 1
}
Write-Output "OK: only administrators and SYSTEM can write to the managed install (scripts, runtime, settings)."

# Outside the install, but still in the local boundary: the shell that runs the hook commands, the
# git they call, and the interpreter the tests run on. If non-admins can write these, the local
# layer only catches mistakes; CI and your merge remain the guarantee.
$bash = if ($env:CLAUDE_CODE_GIT_BASH_PATH) { $env:CLAUDE_CODE_GIT_BASH_PATH } else { Join-Path (Split-Path -Parent (Split-Path -Parent $Git)) "bin\bash.exe" }
$warn = @()
foreach ($p in @($bash, $Git, $Python)) {
    if (Test-Path $p) { $warn += Get-NonAdminWriters $p } else { $warn += "${p}: not found" }
}
if ($warn.Count -gt 0) {
    Write-Output "WARNING: these are writable by non-admins, so the local hooks catch mistakes but are not tamper-proof:"
    $warn | ForEach-Object { Write-Output "  $_" }
    Write-Output "  To close this, install Git for Windows under C:\Program Files\Git and rerun with -Git pointing there."
}
Write-Output "Restart Claude Code sessions to load the hooks."
