$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = $env:PERSONAL_AGENT_PYTHON
$SessionId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
$CurrentLogDir = Join-Path $ProjectRoot "logs\current\$SessionId"
New-Item -ItemType Directory -Path $CurrentLogDir -Force | Out-Null
$BootstrapLog = Join-Path $CurrentLogDir "bootstrap.log"

function Write-Bootstrap {
    param([Parameter(Mandatory)][string]$Message)
    Add-Content -LiteralPath $BootstrapLog -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message) -Encoding utf8
}

Write-Bootstrap "启动会话 $SessionId"

if (-not $PythonExe) {
    $fixed = "D:\miniconda\envs\langchain1.2\python.exe"
    if (Test-Path -LiteralPath $fixed) { $PythonExe = $fixed }
}

if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "未找到 Python。请设置 PERSONAL_AGENT_PYTHON 为 Python 3.13 解释器绝对路径。"
}

$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$interpreter = (& $PythonExe -c "import sys; print(sys.executable)").Trim()
if ((Resolve-Path -LiteralPath $interpreter).Path -ne $PythonExe) {
    throw "Python 解释器不匹配，必须使用：D:\miniconda\envs\langchain1.2\python.exe"
}
Write-Bootstrap "解释器检查通过：Python 3.13"
$pythonVersion = (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($pythonVersion -ne "3.13") {
    throw "Python 版本不匹配：$pythonVersion；必须是 3.13"
}

& (Join-Path $PSScriptRoot "migrate.ps1") -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，停止启动" }
Write-Bootstrap "数据库迁移完成"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PERSONAL_AGENT_LOG_SESSION_ID = $SessionId
$env:PERSONAL_AGENT_LOG_DIR = $CurrentLogDir
Start-Process -FilePath $PythonExe -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory (Join-Path $ProjectRoot "backend") -WindowStyle Hidden
Write-Bootstrap "后端进程已请求启动：127.0.0.1:8000"

$ViteExe = Join-Path $ProjectRoot "frontend\node_modules\.bin\vite.cmd"
if (-not (Test-Path -LiteralPath $ViteExe)) {
    throw "前端依赖未安装，请先在 frontend 目录运行 pnpm install"
}
Start-Process -FilePath $ViteExe -ArgumentList @("--host", "127.0.0.1", "--port", "5176") `
    -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden
Write-Bootstrap "前端进程已请求启动：127.0.0.1:5176"

Write-Host "PersonalAgent 后端：http://127.0.0.1:8000/docs"
Write-Host "PersonalAgent 前端：http://127.0.0.1:5176"
Write-Host "日志会话：$SessionId（停止后归档到 logs/archives）"
