$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $PSScriptRoot '.env'
$envExample = Join-Path $PSScriptRoot '.env.example'
if (-not (Test-Path -LiteralPath $envFile)) {
  Copy-Item -LiteralPath $envExample -Destination $envFile
}
$python = Join-Path $root 'register_env\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & python -m venv (Join-Path $root 'register_env')
}

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt -r requirements-dev.txt
& npm.cmd ci

$mongoUriLine = Get-Content -LiteralPath $envFile |
  Where-Object { $_ -match '^AUTOREGISTER_MONGO_URI=' } |
  Select-Object -Last 1
$mongoUri = if ($mongoUriLine) { ($mongoUriLine -split '=', 2)[1].Trim() } else { '' }
if (-not $mongoUri -or $mongoUri -match 'mongodb(?:\+srv)?://(?:[^@/]+@)?(?:127\.0\.0\.1|localhost)(?::27017)?(?:/|$)') {
  & (Join-Path $PSScriptRoot 'scripts\install-mongodb-service.ps1')
} else {
  Write-Host 'Remote MongoDB configured; skipping local MongoDB installation.'
}

Write-Host 'Setup complete. Run AutoRegister: Full stack in VS Code.'
