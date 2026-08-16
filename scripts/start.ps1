$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = $env:PERSONAL_AGENT_PYTHON

if (-not $PythonExe -and $env:CONDA_PREFIX) {
    $candidate = Join-Path $env:CONDA_PREFIX "python.exe"
    if (Test-Path -LiteralPath $candidate) { $PythonExe = $candidate }
}
if (-not $PythonExe) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $PythonExe = $command.Source }
}

if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "未找到 Python。请设置 PERSONAL_AGENT_PYTHON 为 Python 3.13 解释器绝对路径。"
}

$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

Start-Process -FilePath $PythonExe -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory (Join-Path $ProjectRoot "backend") -WindowStyle Hidden

$ViteExe = Join-Path $ProjectRoot "frontend\node_modules\.bin\vite.cmd"
if (-not (Test-Path -LiteralPath $ViteExe)) {
    throw "前端依赖未安装，请先在 frontend 目录运行 pnpm install"
}
Start-Process -FilePath $ViteExe -ArgumentList @("--host", "127.0.0.1", "--port", "5173") `
    -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden

Write-Host "PersonalAgent 后端：http://127.0.0.1:8000/docs"
Write-Host "PersonalAgent 前端：http://127.0.0.1:5173"
