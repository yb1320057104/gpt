param([int]$Port = 3211)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$listenerPids = @($listeners | ForEach-Object { $_.OwningProcess })
if (-not $listenerPids) {
    $listenerPids = @(netstat -ano -p TCP | ForEach-Object {
        if ($_ -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            [int]$Matches[1]
        }
    })
}
foreach ($listenerPid in ($listenerPids | Sort-Object -Unique)) {
    Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
}
$pidFile = Join-Path $root "data\server.pid"
if (Test-Path $pidFile) {
    $savedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force
}
Write-Output "MailCom Manager stopped"
