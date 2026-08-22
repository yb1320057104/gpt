param(
    [string] $ResinSource = $env:RESIN_ROOT
)

$ErrorActionPreference = 'Stop'

$App = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ProjectRoot = Split-Path -Parent $App
$ProjectBase = Split-Path -Parent (Split-Path -Parent $ProjectRoot)

if (-not $ResinSource) {
    $ResinSource = Get-ChildItem -LiteralPath $ProjectBase -Directory | ForEach-Object {
        $candidate = Join-Path $_.FullName 'Resin'
        if (Test-Path -LiteralPath (Join-Path $candidate 'cmd\resin')) { $candidate }
    } | Select-Object -First 1
}
if (-not $ResinSource -or -not (Test-Path -LiteralPath (Join-Path $ResinSource 'cmd\resin'))) {
    throw 'Resin source was not found. Set RESIN_ROOT to the Resin source directory.'
}
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw 'Go was not found. Install Go before building Resin.'
}

$TargetDirectory = Join-Path $App 'proxy-engines\bin'
$Target = Join-Path $TargetDirectory 'resin.exe'
New-Item -ItemType Directory -Force -Path $TargetDirectory | Out-Null

Push-Location $ResinSource
try {
    & go build -trimpath -tags 'with_quic with_wireguard with_grpc with_utls' -o $Target ./cmd/resin
    if ($LASTEXITCODE -ne 0) {
        throw "Resin build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$item = Get-Item -LiteralPath $Target
Write-Host "Resin built with uTLS/QUIC support: $($item.FullName)" -ForegroundColor Green
