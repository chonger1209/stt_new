param([Parameter(Mandatory=$true)][string]$Root,
      [Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$TaskName,
      [switch]$AtLogOn)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$windowlessPython = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowlessPython)) { throw 'pythonw.exe missing; refusing visible console host' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; refusing to overwrite'
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $windowlessPython -Argument ('-B "' + $Root + '\scripts\service_host.py"') -WorkingDirectory $Root
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$params = @{TaskName=$TaskName; Action=$action; Principal=$principal; Settings=$settings; Description='Independent Qwen receiver host; manual stop state is respected; no client-exit shutdown'}
if ($AtLogOn) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT30S'
    $params.Trigger = $trigger
}
Register-ScheduledTask @params | Select-Object TaskName, State
