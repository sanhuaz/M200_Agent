$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = $env:PERSONAL_AGENT_PYTHON

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
$pythonVersion = (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($pythonVersion -ne "3.13") {
    throw "Python 版本不匹配：$pythonVersion；必须是 3.13"
}

& (Join-Path $PSScriptRoot "migrate.ps1") -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，停止启动" }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Start-Process -FilePath $PythonExe -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory (Join-Path $ProjectRoot "backend") -WindowStyle Hidden

$ViteExe = Join-Path $ProjectRoot "frontend\node_modules\.bin\vite.cmd"
if (-not (Test-Path -LiteralPath $ViteExe)) {
    throw "前端依赖未安装，请先在 frontend 目录运行 pnpm install"
}
Start-Process -FilePath $ViteExe -ArgumentList @("--host", "127.0.0.1", "--port", "5176") `
    -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden

Write-Host "PersonalAgent 后端：http://127.0.0.1:8000/docs"
Write-Host "PersonalAgent 前端：http://127.0.0.1:5176"
