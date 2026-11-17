<#
.SYNOPSIS
    claude-sentinel-suite installer for Windows PowerShell 5.1+ and PowerShell 7.

.DESCRIPTION
    Links the skill into ~/.claude/skills/sentinel-suite (a directory junction, no admin
    rights needed, so `git pull` updates it) and copies the five agents into ~/.claude/agents.

.PARAMETER WithHooks
    Also register the secret-guard (PreToolUse) and handoff (PreCompact, SessionStart) hooks
    in ~/.claude/settings.json. A backup of settings.json is written first.

.PARAMETER Uninstall
    Remove everything this script installed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithHooks
#>
[CmdletBinding()]
param(
    [switch]$WithHooks,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$RepoDir = $PSScriptRoot
if ($env:CLAUDE_CONFIG_DIR) { $ClaudeDir = $env:CLAUDE_CONFIG_DIR } else { $ClaudeDir = Join-Path $HOME '.claude' }
$SkillName = 'sentinel-suite'
$SkillsDir = Join-Path $ClaudeDir 'skills'
$SkillDir = Join-Path $SkillsDir $SkillName
$AgentsDir = Join-Path $ClaudeDir 'agents'
$SettingsPath = Join-Path $ClaudeDir 'settings.json'

function Write-Ok([string]$Message) { Write-Host '  [ok] ' -ForegroundColor Green -NoNewline; Write-Host $Message }
function Write-Note([string]$Message) { Write-Host "  $Message" -ForegroundColor DarkGray }
function Write-Warn([string]$Message) { Write-Host '  [!] ' -ForegroundColor Yellow -NoNewline; Write-Host $Message }

function Find-Python {
    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $exe = & $cmd.Source -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    }
    return $null
}

function Test-ReparsePoint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Remove-Link([string]$Path) {
    # Deletes a junction/symlink itself, never the directory it points to.
    if (Test-ReparsePoint $Path) { [IO.Directory]::Delete($Path, $false) }
}

function Backup-IfReal([string]$Path) {
    if ((Test-Path -LiteralPath $Path) -and -not (Test-ReparsePoint $Path)) {
        $backup = "$Path.backup.$(Get-Date -Format 'yyyyMMddHHmmss')"
        Move-Item -LiteralPath $Path -Destination $backup
        Write-Warn "existing $(Split-Path $Path -Leaf) moved to $backup"
    }
}

Write-Host ''
Write-Host '  claude-sentinel-suite' -ForegroundColor White -NoNewline
Write-Host "  -> $ClaudeDir" -ForegroundColor DarkGray
Write-Host ''

$Python = Find-Python
if (-not $Python) {
    Write-Host '  Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick "Add to PATH").' -ForegroundColor Red
    exit 1
}

$AgentFiles = Get-ChildItem -Path (Join-Path $RepoDir 'agents') -Filter '*.md'

if ($Uninstall) {
    if (Test-ReparsePoint $SkillDir) {
        Remove-Link $SkillDir
        Write-Ok "removed skill link $SkillDir"
    }
    foreach ($agent in $AgentFiles) {
        $target = Join-Path $AgentsDir $agent.Name
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
            Write-Ok "removed agent $($agent.BaseName)"
        }
    }
    if (Test-Path -LiteralPath $SettingsPath) {
        & $Python (Join-Path $RepoDir 'hooks\register_hooks.py') --uninstall --settings $SettingsPath
    }
    Write-Host ''
    Write-Host '  Uninstalled. Restart Claude Code to apply.' -ForegroundColor Green
    Write-Host ''
    exit 0
}

New-Item -ItemType Directory -Force -Path $SkillsDir, $AgentsDir | Out-Null

Remove-Link $SkillDir
Backup-IfReal $SkillDir
New-Item -ItemType Junction -Path $SkillDir -Target $RepoDir | Out-Null
Write-Ok "skill    $SkillName -> $SkillDir"

foreach ($agent in $AgentFiles) {
    $target = Join-Path $AgentsDir $agent.Name
    if ((Test-Path -LiteralPath $target) -and -not (Test-ReparsePoint $target)) {
        $same = (Get-FileHash -LiteralPath $target).Hash -eq (Get-FileHash -LiteralPath $agent.FullName).Hash
        if (-not $same) { Copy-Item -LiteralPath $target -Destination "$target.bak" -Force }
    }
    Copy-Item -LiteralPath $agent.FullName -Destination $target -Force
}
Write-Ok "agents   $($AgentFiles.Count) Haiku micro-agents -> $AgentsDir"
Write-Note '         (agents are copied on Windows: re-run install.ps1 after git pull)'

if ($WithHooks) {
    & $Python (Join-Path $RepoDir 'hooks\register_hooks.py') --settings $SettingsPath --root $SkillDir --python $Python
    if ($LASTEXITCODE -ne 0) { Write-Host '  hook registration failed (see above)' -ForegroundColor Red; exit 1 }
    Write-Ok 'hooks    secret-guard (PreToolUse) + handoff (PreCompact, SessionStart)'
} else {
    Write-Note 'hooks    skipped (re-run with -WithHooks to enable secret-guard + auto-handoff)'
}

Write-Host ''
Write-Host '  Done. Restart Claude Code, then try:' -ForegroundColor Green
Write-Host ''
Write-Host "    python `"$SkillDir\scripts\context_diet.py`" --demo"
Write-Host "    python `"$SkillDir\scripts\spiderweb.py`" --target . --open"
Write-Host "    python `"$SkillDir\scripts\nightshift.py`" --dry-run"
Write-Host ''
