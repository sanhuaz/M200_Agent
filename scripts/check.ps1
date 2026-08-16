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

& $PythonExe -m pip check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExe -m compileall -q (Join-Path $ProjectRoot "backend")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExe -m ruff check (Join-Path $ProjectRoot "backend")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExe -m pyright --pythonpath $PythonExe (Join-Path $ProjectRoot "backend\app")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExe -m pytest -q (Join-Path $ProjectRoot "backend\tests")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location (Join-Path $ProjectRoot "frontend")
try {
    & ".\node_modules\.bin\vue-tsc.cmd" --noEmit
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & ".\node_modules\.bin\vite.cmd" build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
