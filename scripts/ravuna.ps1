$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = if ($env:RAVUNA_PYTHON) { $env:RAVUNA_PYTHON } else { Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python -m app.content_studio.cli @args
exit $LASTEXITCODE
