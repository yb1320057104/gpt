param(
    [switch] $SkipMailCom,
    [switch] $NoBrowser,
    [switch] $Restart
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root 'app'
$Python = Join-Path $Root 'register_env\Scripts\python.exe'
$settingsPath = Join-Path $Root 'data\settings.json'
$RuntimeLogs = Join-Path $Root 'data\runtime'
$RuntimePids = Join-Path $RuntimeLogs 'pids'
$MailComStart = Join-Path $Root 'mailcom-manager\start.ps1'
$AppEnv = Join-Path $App '.env'
$ProjectBase = Split-Path -Parent (Split-Path -Parent $Root)
$EasyProxiesRoot = if ($env:EASY_PROXIES_ROOT) {
    $env:EASY_PROXIES_ROOT
} else {
    Get-ChildItem -LiteralPath $ProjectBase -Directory | ForEach-Object {
        $candidate = Join-Path $_.FullName 'easy-proxies'
        if (Test-Path -LiteralPath (Join-Path $candidate 'easy_proxies.exe')) { $candidate }
    } | Select-Object -First 1
}
$ResinRoot = if ($env:RESIN_ROOT) {
    $env:RESIN_ROOT
} else {
    Get-ChildItem -LiteralPath $ProjectBase -Directory | ForEach-Object {
        $candidate = Join-Path $_.FullName 'Resin'
        if (Test-Path -LiteralPath (Join-Path $candidate 'resin.exe')) { $candidate }
    } | Select-Object -First 1
}
New-Item -ItemType Directory -Force -Path $RuntimeLogs, $RuntimePids | Out-Null

if ($Restart) {
    & (Join-Path $Root 'stop-autoregister.ps1') -KeepRoxy:$false
}
$Roxy = $env:ROXY_BROWSER_PATH
if (-not $Roxy -and (Test-Path -LiteralPath $settingsPath)) {
    try {
        $Roxy = (Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json).browserExecutablePath
    } catch {
        Write-Host '  settings.json could not be read; RoxyBrowser will not be auto-started.' -ForegroundColor Yellow
    }
}

function Test-LocalPort([int] $Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(1000)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-Component([string] $Name, [scriptblock] $Action) {
    Write-Host "[$Name] checking..." -ForegroundColor Cyan
    & $Action
}

function Save-ProcessId([string] $Name, [System.Diagnostics.Process] $Process) {
    if ($Process -and -not $Process.HasExited) {
        $Process.Id | Set-Content -LiteralPath (Join-Path $RuntimePids "$Name.pid") -Encoding ascii
    }
}
if (-not $Roxy -or -not (Test-Path -LiteralPath $Roxy)) {
    $Roxy = @(
        'D:\app\RoxyBrowser\RoxyBrowser.exe',
        'D:\RoxyBrowser\RoxyBrowser.exe',
        (Join-Path $env:LOCALAPPDATA 'RoxyBrowser\RoxyBrowser.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Wait-LocalPort([int] $Port, [int] $TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (Test-LocalPort $Port) { return $true }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Confirm-Started(
    [string] $Name,
    [System.Diagnostics.Process] $Process,
    [int] $Port,
    [string] $ErrorLog,
    [int] $TimeoutSeconds = 20
) {
    if (Wait-LocalPort $Port $TimeoutSeconds) {
        Write-Host "  ready on 127.0.0.1:$Port (PID $($Process.Id))" -ForegroundColor Green
        return
    }

    $Process.Refresh()
    if ($Process.HasExited) {
        Write-Host "  failed: process exited with code $($Process.ExitCode)" -ForegroundColor Red
    } else {
        Write-Host "  failed: port $Port was not ready after $TimeoutSeconds seconds (PID $($Process.Id))" -ForegroundColor Red
    }
    if ($ErrorLog) {
        Write-Host "  error log: $ErrorLog" -ForegroundColor Yellow
    }
}

function Get-AppEnvValue([string] $Name) {
    if (-not (Test-Path -LiteralPath $AppEnv)) { return '' }
    $line = Get-Content -LiteralPath $AppEnv | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim()
}

function Ensure-AppSecret([string] $Name) {
    $value = Get-AppEnvValue $Name
    if ($value) { return $value }
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $value = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    Add-Content -LiteralPath $AppEnv -Value "$Name=$value"
    return $value
}

if (-not (Test-Path -LiteralPath $AppEnv)) {
    New-Item -ItemType File -Path $AppEnv -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found. Run app\setup.ps1 first: $Python"
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'npm.cmd was not found. Install Node.js before starting the frontend.'
}

Start-Component 'Easy Proxies' {
    $binary = if ($EasyProxiesRoot) { Join-Path $EasyProxiesRoot 'easy_proxies.exe' } else { '' }
    $safeConfig = Join-Path $App 'proxy-engines\easy-proxies.yaml'
    if (Test-LocalPort 9091) {
        Write-Host '  already running on 127.0.0.1:9091' -ForegroundColor Green
    } elseif ($binary -and (Test-Path -LiteralPath $binary) -and (Test-Path -LiteralPath $safeConfig)) {
        $process = Start-Process -FilePath $binary -ArgumentList '-config', $safeConfig -WorkingDirectory (Split-Path -Parent $safeConfig) -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $RuntimeLogs 'easy-proxies.out.log') `
            -RedirectStandardError (Join-Path $RuntimeLogs 'easy-proxies.err.log')
        Confirm-Started 'Easy Proxies' $process 9091 (Join-Path $RuntimeLogs 'easy-proxies.err.log') 15
        Save-ProcessId 'easy-proxies' $process
    } else {
        Write-Host '  executable or managed config not found; set EASY_PROXIES_ROOT and verify app\proxy-engines\easy-proxies.yaml' -ForegroundColor Yellow
    }
}

Start-Component 'Resin' {
    $managedBinary = Join-Path $App 'proxy-engines\bin\resin.exe'
    $binary = if (Test-Path -LiteralPath $managedBinary) { $managedBinary } elseif ($ResinRoot) { Join-Path $ResinRoot 'resin.exe' } else { '' }
    $workingDirectory = if (Test-Path -LiteralPath $managedBinary) { Split-Path -Parent $managedBinary } else { $ResinRoot }
    if (Test-LocalPort 2260) {
        Write-Host '  already running on 127.0.0.1:2260' -ForegroundColor Green
    } elseif ($binary -and $workingDirectory -and (Test-Path -LiteralPath $binary)) {
        $adminToken = Ensure-AppSecret 'AUTOREGISTER_RESIN_ADMIN_TOKEN'
        $proxyToken = Ensure-AppSecret 'AUTOREGISTER_RESIN_PROXY_TOKEN'
        $previousAdmin = $env:RESIN_ADMIN_TOKEN
        $previousProxy = $env:RESIN_PROXY_TOKEN
        $previousAddress = $env:RESIN_LISTEN_ADDRESS
        $previousPort = $env:RESIN_PORT
        $previousCache = $env:RESIN_CACHE_DIR
        $previousState = $env:RESIN_STATE_DIR
        $previousLog = $env:RESIN_LOG_DIR
        try {
            $env:RESIN_ADMIN_TOKEN = $adminToken
            $env:RESIN_PROXY_TOKEN = $proxyToken
            $env:RESIN_LISTEN_ADDRESS = '127.0.0.1'
            $env:RESIN_PORT = '2260'
            $env:RESIN_CACHE_DIR = Join-Path $Root 'data\resin\cache'
            $env:RESIN_STATE_DIR = Join-Path $Root 'data\resin\state'
            $env:RESIN_LOG_DIR = Join-Path $Root 'data\resin\log'
            New-Item -ItemType Directory -Force -Path $env:RESIN_CACHE_DIR, $env:RESIN_STATE_DIR, $env:RESIN_LOG_DIR | Out-Null
            $process = Start-Process -FilePath $binary -WorkingDirectory $workingDirectory -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $RuntimeLogs 'resin.out.log') `
                -RedirectStandardError (Join-Path $RuntimeLogs 'resin.err.log')
        } finally {
            $env:RESIN_ADMIN_TOKEN = $previousAdmin
            $env:RESIN_PROXY_TOKEN = $previousProxy
            $env:RESIN_LISTEN_ADDRESS = $previousAddress
            $env:RESIN_PORT = $previousPort
            $env:RESIN_CACHE_DIR = $previousCache
            $env:RESIN_STATE_DIR = $previousState
            $env:RESIN_LOG_DIR = $previousLog
        }
        Confirm-Started 'Resin' $process 2260 (Join-Path $RuntimeLogs 'resin.err.log') 15
        Save-ProcessId 'resin' $process
    } else {
        Write-Host "  executable not found: $binary (build with: go build -o resin.exe .\cmd\resin)" -ForegroundColor Yellow
    }
}

Start-Component 'RoxyBrowser' {
    if (Get-Process -Name 'RoxyBrowser' -ErrorAction SilentlyContinue) {
        Write-Host '  already running' -ForegroundColor Green
    } elseif ($Roxy -and (Test-Path -LiteralPath $Roxy)) {
        $process = Start-Process -FilePath $Roxy -WindowStyle Normal -PassThru
        Save-ProcessId 'roxy-browser' $process
        Write-Host '  started; API may need a few seconds to become ready' -ForegroundColor Green
    } else {
        Write-Host '  path is not configured; start RoxyBrowser manually or set ROXY_BROWSER_PATH' -ForegroundColor Yellow
    }
}

Start-Component 'MongoDB' {
    $configuredMongo = Get-AppEnvValue 'AUTOREGISTER_MONGO_URI'
    if ($configuredMongo -and $configuredMongo -notmatch 'mongodb(?:\+srv)?://(?:[^@/]+@)?(?:127\.0\.0\.1|localhost)(?::27017)?(?:/|$)') {
        Write-Host '  remote MongoDB configured in app/.env; local service skipped' -ForegroundColor Green
    } elseif (Test-LocalPort 27017) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        & (Join-Path $App 'scripts\start-mongodb.ps1')
        if ($LASTEXITCODE -ne 0) { throw "MongoDB startup failed with exit code $LASTEXITCODE" }
    }
}

Start-Component 'Backend' {
    if (Test-LocalPort 8000) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        $previousLogEnqueue = $env:OPLL_LOG_ENQUEUE
        $env:OPLL_LOG_ENQUEUE = 'false'
        try {
            $process = Start-Process -FilePath $Python -ArgumentList '-m', 'backend' -WorkingDirectory $App -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $RuntimeLogs 'autoregister-backend.out.log') `
                -RedirectStandardError (Join-Path $RuntimeLogs 'autoregister-backend.err.log')
        } finally {
            if ($null -eq $previousLogEnqueue) {
                Remove-Item Env:\OPLL_LOG_ENQUEUE -ErrorAction SilentlyContinue
            } else {
                $env:OPLL_LOG_ENQUEUE = $previousLogEnqueue
            }
        }
        Confirm-Started 'Backend' $process 8000 (Join-Path $RuntimeLogs 'autoregister-backend.err.log') 30
        Save-ProcessId 'backend' $process
    }
}

Start-Component 'Frontend' {
    if (Test-LocalPort 5173) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        $process = Start-Process -FilePath 'npm.cmd' `
            -ArgumentList 'run', 'dev', '--', '--configLoader', 'native', '--host', '127.0.0.1' `
            -WorkingDirectory $App -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $RuntimeLogs 'autoregister-frontend.out.log') `
            -RedirectStandardError (Join-Path $RuntimeLogs 'autoregister-frontend.err.log')
        Confirm-Started 'Frontend' $process 5173 (Join-Path $RuntimeLogs 'autoregister-frontend.err.log') 30
        Save-ProcessId 'frontend' $process
    }
}

if (-not $SkipMailCom) {
    Start-Component 'MailCom' {
        if (Test-LocalPort 3211) {
            Write-Host '  already running' -ForegroundColor Green
        } elseif (Test-Path -LiteralPath $MailComStart) {
            & $MailComStart -NoBrowser
        } else {
            Write-Host '  manager is not installed; non-MailCom mailboxes remain available' -ForegroundColor Yellow
        }
    }
}

Write-Host ''
Write-Host 'AutoRegister started:' -ForegroundColor Green
Write-Host '  Page: http://127.0.0.1:5173/launch'
Write-Host '  Backend: http://127.0.0.1:8000'
Write-Host '  Roxy API: http://127.0.0.1:50000 (enable it in Roxy first)'
Write-Host '  Proxy bridge: starts on demand at http://127.0.0.1:18796'
if (Test-LocalPort 7890) {
    Write-Host '  Local proxy: ready at 127.0.0.1:7890' -ForegroundColor Green
} else {
    Write-Host '  Local proxy: unavailable (enable Clash/Mihomo mixed port 7890 before selecting local proxy)' -ForegroundColor Yellow
}

if (-not $NoBrowser -and (Test-LocalPort 5173)) {
    Start-Process 'http://127.0.0.1:5173/launch'
}
if (-not $SkipMailCom) {
    Write-Host '  MailCom: http://127.0.0.1:3211'
}

try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 5
    Write-Host "  Backend status: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host '  Backend is still starting; refresh in a few seconds.' -ForegroundColor Yellow
}

try {
    $null = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/launch' -UseBasicParsing -TimeoutSec 5
    Write-Host '  Frontend status: ready' -ForegroundColor Green
} catch {
    Write-Host "  Frontend is still starting; check $RuntimeLogs\autoregister-frontend.err.log" -ForegroundColor Yellow
}

if (-not $SkipMailCom) {
    try {
        $mailHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:3211/api/health' -TimeoutSec 5
        Write-Host "  MailCom status: $($mailHealth.status)" -ForegroundColor Green
    } catch {
        Write-Host '  MailCom is still starting; registration can use other mailbox sources.' -ForegroundColor Yellow
    }
}
