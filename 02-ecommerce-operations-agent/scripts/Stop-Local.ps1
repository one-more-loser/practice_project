$ErrorActionPreference = 'Stop'
$opsRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ((Split-Path $opsRoot -Leaf) -ne '02-ecommerce-operations-agent') { throw '目录校验失败' }
$opsRecord = Join-Path $opsRoot '.runtime\local-processes.json'
if (!(Test-Path -LiteralPath $opsRecord)) { Write-Output '没有本启动脚本登记的进程'; return }
foreach ($opsEntry in (Get-Content -Raw -LiteralPath $opsRecord | ConvertFrom-Json)) {
    $opsProcess = Get-Process -Id $opsEntry.pid -ErrorAction SilentlyContinue
    if (!$opsProcess) { continue }
    $opsStarted = $opsProcess.StartTime.ToUniversalTime().Ticks
    $opsRecordedStart = ([datetime]$opsEntry.started).ToUniversalTime().Ticks
    $opsCommand = (Get-CimInstance Win32_Process -Filter "ProcessId=$($opsEntry.pid)").CommandLine
    $opsOwned = ($opsStarted -eq $opsRecordedStart -and $opsCommand -and
        $opsCommand.IndexOf($opsRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    if (!$opsOwned) { throw "进程 $($opsEntry.pid) 身份不符，拒绝结束" }
    Stop-Process -Id $opsEntry.pid
}
Write-Output '已结束本脚本创建且身份匹配的02进程；演示库保留'
