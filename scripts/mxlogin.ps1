<#
.SYNOPSIS
  Change the bridge's Matrix/Element account from this machine.

.DESCRIPTION
  A thin wrapper: the prompts and the login itself run ON THE BRIDGE HOST, this
  script only carries your keystrokes there over SSH. That is the point - the
  homeserver records the IP of whoever calls /login, and it must never be this
  laptop. Your password travels inside the SSH tunnel and is never written to
  disk, an argument list, or shell history.

  With no -Server it falls back to a local `docker compose` (dev only).

.PARAMETER User
  The account to log in as, e.g. @alice:matrix.org. Pass this to SWITCH
  accounts - without it the tool defaults to (and only re-logs-in) the account
  already configured, which is why "there was nowhere to type the account".

.PARAMETER Homeserver
  Homeserver URL, e.g. https://matrix.org. Defaults to the configured one.

.PARAMETER Room
  Control room id/alias to join, e.g. !abc:matrix.org or #room:matrix.org.

.PARAMETER Token
  Use an existing access token (SSO accounts) instead of a password.

.EXAMPLE
  .\scripts\mxlogin.ps1 -Server root@vps.example.com
  .\scripts\mxlogin.ps1                      # local Docker
  .\scripts\mxlogin.ps1 -User '@alice:matrix.org' -Room '!ctl:matrix.org'

.NOTES
  Defaults can live in the environment instead of the command line:
    $env:BRIDGE_SSH_HOST    = "root@vps.example.com"
    $env:BRIDGE_REMOTE_PATH = "/srv/matrix-telegram-bridge"
#>
[CmdletBinding()]
param(
    [string]$Server     = $env:BRIDGE_SSH_HOST,
    [string]$RemotePath = $(if ($env:BRIDGE_REMOTE_PATH) { $env:BRIDGE_REMOTE_PATH } else { "/srv/matrix-telegram-bridge" }),
    [string]$Config     = "/config/config.yaml",
    [string]$User       = "",
    [string]$Homeserver = "",
    [string]$Room       = "",
    [switch]$Token,
    [switch]$SkipEgressCheck
)

$ErrorActionPreference = "Stop"

$inner = @(
    "docker", "compose", "run", "--rm",
    "--entrypoint", "python", "bridge",
    "-m", "bridge.mxlogin", "--config", $Config
)
# Forwarded so the account is set by flag, not left to an interactive prompt
# that a non-tty stdin would silently skip (the cause of "only asked password").
if ($User)       { $inner += @("--user", $User) }
if ($Homeserver) { $inner += @("--homeserver", $Homeserver) }
if ($Room)       { $inner += @("--room", $Room) }
if ($Token)      { $inner += "--token" }
if ($SkipEgressCheck) { $inner += "--no-egress-check" }

if (-not $Server) {
    Write-Host "no -Server given -> running against LOCAL docker" -ForegroundColor Yellow
    Write-Host "(the homeserver will see THIS machine's egress address)" -ForegroundColor Yellow
    $repo = Split-Path -Parent $PSScriptRoot
    $exe  = $inner[0]
    $rest = $inner[1..($inner.Length - 1)]   # splatted below, not passed as one array
    Push-Location $repo
    try { & $exe @rest }
    finally { Pop-Location }
    exit $LASTEXITCODE
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh not found. Install OpenSSH client (Settings > Optional Features)."
}

# -t forces a remote TTY so getpass() can turn off echo for the password.
$remoteCmd = "cd '$RemotePath' && $($inner -join ' ')"
Write-Host "connecting to $Server ..." -ForegroundColor Cyan
& ssh -t $Server $remoteCmd
exit $LASTEXITCODE
