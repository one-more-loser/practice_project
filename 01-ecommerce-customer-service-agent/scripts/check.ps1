param(
    [string]$Node = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe",
    [string]$NpmCli = 'C:\Node.js\node_modules\npm\bin\npm-cli.js'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$databaseEnvironmentKeys = @(
    'CS_DATABASE_PROFILE', 'CS_DATABASE_URL', 'CS_MIGRATION_URL',
    'CS_TEST_DATABASE_URL', 'CS_TEST_MIGRATION_URL', 'CS_UPGRADE_MIGRATION_URL',
    'CS_ISOLATION_FILE', 'CS_EXPECTED_DATABASE'
)
$originalDatabaseEnvironment = @{}
foreach ($key in $databaseEnvironmentKeys) {
    $originalDatabaseEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    [Environment]::SetEnvironmentVariable($key, $null, 'Process')
}
$env:PYTHONUTF8 = '1'
$env:CS_DATABASE_PROFILE = 'local'
$env:CS_ISOLATION_FILE = Join-Path $projectRoot '.runtime\isolation.json'
$env:CS_EXPECTED_DATABASE = 'cs_agent_b0'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:UV_CACHE_DIR = Join-Path $projectRoot '.runtime\uv-cache'
$env:TEMP = Join-Path $projectRoot '.runtime\tmp'
$env:TMP = $env:TEMP
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.runtime\ms-playwright'
$env:PATH = (Split-Path -Parent $Node) + ';' + $env:PATH
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
Push-Location $projectRoot
$databaseStarted = $false
try {
    & $Node -e "if(Number(process.versions.node.split('.')[0])<24)process.exit(1)"
    if ($LASTEXITCODE) { throw '本脚本验证组合使用Node24；请传入已核对的Node路径。' }
    & $pythonExe scripts/dev_database.py start
    if ($LASTEXITCODE) { throw '项目隔离数据库启动或核对失败。' }
    $databaseStarted = $true
    & $pythonExe scripts/migrate.py app
    if ($LASTEXITCODE) { throw '应用库迁移失败。' }
    & $pythonExe scripts/migrate.py test
    if ($LASTEXITCODE) { throw '测试库迁移失败。' }
    & $pythonExe -m ruff check --config backend/pyproject.toml backend/src backend/tests backend/migrations scripts
    if ($LASTEXITCODE) { throw '静态检查失败。' }
    & $pythonExe -m pytest backend/tests -q --tb=short --basetemp=.runtime/pytest-check --junitxml=.runtime/pytest-b1.xml
    if ($LASTEXITCODE) { throw '后端测试失败；未执行后续检查。' }
    & $Node $NpmCli --prefix frontend run build
    if ($LASTEXITCODE) { throw '前端类型或构建检查失败。' }
    & $Node $NpmCli --prefix frontend test -- --reporter=default --reporter=json --outputFile=../.runtime/vitest-b1.json
    if ($LASTEXITCODE) { throw '前端测试失败。' }
    & $pythonExe scripts/verify_b0_http.py
    if ($LASTEXITCODE) { throw '真实HTTP检查失败。' }
    & $Node $NpmCli --prefix frontend run test:e2e
    if ($LASTEXITCODE) { throw '真实浏览器交易检查失败。' }
    & $pythonExe scripts/capture_compat.py --node $Node --stage B1
    if ($LASTEXITCODE) { throw '版本与指纹记录失败。' }
    & $pythonExe scripts/capture_b1_evidence.py
    if ($LASTEXITCODE) { throw '验收证据聚合失败。' }
} finally {
    # 关闭项目实例并撤销临时路径别名，不删除持久数据。
    if ($databaseStarted) { & $pythonExe scripts/dev_database.py stop }
    foreach ($key in $databaseEnvironmentKeys) {
        [Environment]::SetEnvironmentVariable($key, $originalDatabaseEnvironment[$key], 'Process')
    }
    Pop-Location
}
