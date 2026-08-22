param(
    [int]$Port = 3211,
    [string]$ImapHost = "",
    [int]$ImapPort = 0,
    [string]$ImapProxy = "",
    [switch]$DirectImap,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $root
$python = Join-Path $workspaceRoot "register_env\Scripts\python.exe"
$healthUrl = "http://127.0.0.1:$Port/api/health"
$homeUrl = "http://127.0.0.1:$Port/"
$appEnv = Join-Path $workspaceRoot "app\.env"

function Get-AppEnvValue([string] $Name) {
    if (-not (Test-Path -LiteralPath $appEnv)) { return "" }
    $line = Get-Content -LiteralPath $appEnv | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if (-not $line) { return "" }
    return ($line -split "=", 2)[1].Trim()
}

if (-not $env:MAILCOM_MONGO_URI) {
    $env:MAILCOM_MONGO_URI = Get-AppEnvValue "AUTOREGISTER_MONGO_URI"
}
if (-not $env:MAILCOM_MONGO_DATABASE) {
    $env:MAILCOM_MONGO_DATABASE = Get-AppEnvValue "AUTOREGISTER_MONGO_DATABASE"
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found. Run app\setup.ps1 first: $python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$customImap = $PSBoundParameters.ContainsKey("ImapHost") -or
    $PSBoundParameters.ContainsKey("ImapPort") -or
    $PSBoundParameters.ContainsKey("ImapProxy") -or $DirectImap
if ($listener -and $customImap) {
    throw "MailCom Manager is already running. Stop it before changing IMAP settings."
}
if ($DirectImap -and $PSBoundParameters.ContainsKey("ImapProxy")) {
    throw "DirectImap and ImapProxy cannot be used together"
}

if ($ImapHost) {
    $env:MAILCOM_IMAP_HOST = $ImapHost
}
if ($ImapPort) {
    if ($ImapPort -lt 1 -or $ImapPort -gt 65535) {
        throw "IMAP port must be between 1 and 65535"
    }
    $env:MAILCOM_IMAP_PORT = "$ImapPort"
}
if ($PSBoundParameters.ContainsKey("ImapProxy")) {
    $env:MAILCOM_IMAP_PROXY = $ImapProxy
}
if ($DirectImap) {
    Remove-Item Env:MAILCOM_IMAP_PROXY -ErrorAction SilentlyContinue
}

if (-not $listener) {
    $localProxy = Get-NetTCPConnection -LocalPort 7897 -State Listen -ErrorAction SilentlyContinue
    $savedImapSettings = Test-Path -LiteralPath (Join-Path $root "data\imap-settings.json")
    if ($localProxy -and -not $env:MAILCOM_MONGO_URI -and -not $savedImapSettings -and -not $customImap -and -not $env:MAILCOM_IMAP_PROXY) {
        $env:MAILCOM_IMAP_PROXY = "socks5://127.0.0.1:7897"
    }
    $stdout = Join-Path $root "data\server.stdout.log"
    $stderr = Join-Path $root "data\server.stderr.log"
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "uvicorn", "manager.app:app", "--host", "127.0.0.1", "--port", "$Port" `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $root "data\server.pid") -Encoding ascii
}

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {}
}

if (-not $ready) {
    Get-Content -LiteralPath (Join-Path $root "data\server.stderr.log") -Tail 60 -ErrorAction SilentlyContinue
    throw "MailCom Manager did not become healthy"
}

Write-Output "MailCom Manager is ready: $homeUrl"
if (-not $NoBrowser) {
    Start-Process $homeUrl
}
