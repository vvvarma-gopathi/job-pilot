$ErrorActionPreference = "Stop"
$TaskName = "JobPilotAI"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -eq "Running") {
    Write-Output "The JobPilot agent task is already running."
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Write-Output "Started the JobPilot agent task."
