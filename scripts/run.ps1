param([int]$Port = 8001, [string]$Python = "python")
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$env:PYTHONPATH = Join-Path $projectDirectory 'src'
$env:PYTHONUTF8 = '1'
# Explicitly load plain KEY=VALUE values, without evaluating shell expressions.
if (Test-Path -LiteralPath '.env') {
    foreach ($line in Get-Content -Encoding UTF8 -LiteralPath '.env') {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}
& $Python -m uvicorn service_flow.api:app --host 127.0.0.1 --port $Port
