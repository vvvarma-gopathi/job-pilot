param(
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

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

$StopFile = (& $Python -c "from jobpilot_discovery.config import Settings; print(Settings.from_env().db_path.resolve().parent / 'agent.stop')").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($StopFile)) {
    throw "Could not resolve the configured agent stop-file path."
}

$StopDirectory = Split-Path -Parent $StopFile
New-Item -ItemType Directory -Force -Path $StopDirectory | Out-Null
[System.IO.File]::WriteAllText($StopFile, "")

for ($Attempt = 0; $Attempt -lt $TimeoutSeconds; $Attempt++) {
    $Status = & $Python (Join-Path $RepoRoot "main.py") agent status 2>&1
    if ($LASTEXITCODE -ne 0) {
        $Status | ForEach-Object { Write-Output $_ }
        throw "Could not read agent status while waiting for graceful shutdown."
    }
    if ($Status -match "Agent running: no") {
        Write-Output "The JobPilot agent stopped gracefully."
        exit 0
    }
    Start-Sleep -Seconds 1
}

throw "The agent did not release its lock within $TimeoutSeconds seconds. The stop request remains in place; no process was force-terminated."
