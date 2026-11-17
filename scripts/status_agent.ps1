$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $PythonCommand = Get-Command python -ErrorAction Stop
    $Python = $PythonCommand.Source
}

Get-ScheduledTask -TaskName "JobPilotAI" -ErrorAction SilentlyContinue |
    Select-Object TaskName, State
& $Python (Join-Path $RepoRoot "main.py") agent status
if ($LASTEXITCODE -ne 0) {
    throw "Could not read JobPilot agent status."
}
