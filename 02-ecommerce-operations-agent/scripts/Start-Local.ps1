param([int]$BackendPort = 8120, [int]$FrontendPort = 5174)
$ErrorActionPreference = 'Stop'
$opsRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ((Split-Path $opsRoot -Leaf) -ne '02-ecommerce-operations-agent') { throw '目录校验失败' }
foreach ($opsPort in @($BackendPort, $FrontendPort)) {
    if (Get-NetTCPConnection -LocalPort $opsPort -State Listen -ErrorAction SilentlyContinue) {
        throw "端口 $opsPort 已被使用；先核对所属进程，本脚本不会结束其他服务。"
    }
}
$opsPython = Join-Path $opsRoot '.venv\Scripts\python.exe'
$opsVite = Join-Path $opsRoot 'frontend\node_modules\vite\bin\vite.js'
if (!(Test-Path -LiteralPath $opsPython) -or !(Test-Path -LiteralPath $opsVite)) {
    throw '依赖尚未安装，请按README准备02虚拟环境和frontend/node_modules'
}
$opsRuntime = Join-Path $opsRoot '.runtime'
New-Item -ItemType Directory -Path $opsRuntime -Force | Out-Null
$opsNode = (Get-Command node.exe).Source
$opsOldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $opsRoot 'backend\src'
    $opsBackend = Start-Process -FilePath $opsPython -ArgumentList @('-m','ops_agent','serve','--demo',
        '--port', $BackendPort, '--origin', "http://127.0.0.1:$FrontendPort") -WorkingDirectory (Join-Path $opsRoot 'backend') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $opsRuntime 'backend.log') `
        -RedirectStandardError (Join-Path $opsRuntime 'backend-error.log')
    $opsFrontend = Start-Process -FilePath $opsNode -ArgumentList @(('"' + $opsVite + '"'), '--host', '127.0.0.1',
        '--port', $FrontendPort, '--strictPort') -WorkingDirectory (Join-Path $opsRoot 'frontend') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $opsRuntime 'frontend.log') `
        -RedirectStandardError (Join-Path $opsRuntime 'frontend-error.log')
    @(@{pid=$opsBackend.Id; started=$opsBackend.StartTime.ToUniversalTime().ToString('o'); kind='backend'},
      @{pid=$opsFrontend.Id; started=$opsFrontend.StartTime.ToUniversalTime().ToString('o'); kind='frontend'}) |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $opsRuntime 'local-processes.json') -Encoding utf8
} finally { $env:PYTHONPATH = $opsOldPythonPath }
$opsReady = $false
for ($opsAttempt=0; $opsAttempt -lt 20; $opsAttempt++) {
    try {
        $opsHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 1
        $opsPage = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/operations/" -TimeoutSec 1 -UseBasicParsing
        if ($opsHealth.service -eq 'ops02' -and $opsPage.StatusCode -eq 200) { $opsReady=$true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
if (!$opsReady) { throw '服务未就绪，请查02/.runtime日志。进程已登记，可用Stop-Local清理。' }
Write-Output "02演示已就绪：http://127.0.0.1:$FrontendPort/operations/（合成数据；01真实执行未接入）"
