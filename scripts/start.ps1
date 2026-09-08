param(
    [string]$PythonExe,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5176
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SessionId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
$CurrentLogDir = Join-Path $ProjectRoot "logs\current\$SessionId"
New-Item -ItemType Directory -Path $CurrentLogDir -Force | Out-Null
$BootstrapLog = Join-Path $CurrentLogDir "bootstrap.log"
$FallbackBackendPort = 8200

function Write-Bootstrap {
    param([Parameter(Mandatory)][string]$Message)
    Add-Content -LiteralPath $BootstrapLog -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message) -Encoding utf8
}

function Test-LoopbackPortBindable {
    param([Parameter(Mandatory)][int]$Port)

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $Port
    )
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        try { $listener.Stop() } catch { }
    }
}

Write-Bootstrap "启动会话 $SessionId"

if (-not (Test-LoopbackPortBindable -Port $BackendPort)) {
    if ($BackendPort -eq 8000 -and (Test-LoopbackPortBindable -Port $FallbackBackendPort)) {
        Write-Warning "后端端口 8000 不可绑定，自动回退到 $FallbackBackendPort。"
        Write-Bootstrap "后端端口 8000 不可绑定，回退到 $FallbackBackendPort"
        $BackendPort = $FallbackBackendPort
    } else {
        throw "后端端口 $BackendPort 不可绑定，请释放端口或使用 -BackendPort 指定其他端口。"
    }
}
if (-not (Test-LoopbackPortBindable -Port $FrontendPort)) {
    throw "前端端口 $FrontendPort 不可绑定，请释放端口或使用 -FrontendPort 指定其他端口。"
}

$Interactive = -not [Console]::IsInputRedirected
$Resolver = Join-Path $PSScriptRoot "python-resolver.ps1"
$RequiredModules = @(
    "alembic", "bs4", "chromadb", "docx", "fastapi", "httpx", "jieba", "jmcomic",
    "langchain", "langchain_openai", "langgraph", "mcp", "multipart", "PIL", "pydantic_settings",
    "pypdf", "sentence_transformers", "sqlalchemy", "uvicorn"
)
$PythonExe = (
    & $Resolver -RequestedPath $PythonExe -Interactive:$Interactive -RequiredModules $RequiredModules |
        Select-Object -Last 1
).Trim()
Write-Bootstrap "解释器检查通过：$PythonExe（Python 3.13）"

& (Join-Path $PSScriptRoot "migrate.ps1") -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，停止启动" }
Write-Bootstrap "数据库迁移完成"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PERSONAL_AGENT_LOG_SESSION_ID = $SessionId
$env:PERSONAL_AGENT_LOG_DIR = $CurrentLogDir
$env:PERSONAL_AGENT_BACKEND_PORT = [string]$BackendPort
Start-Process -FilePath $PythonExe -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", [string]$BackendPort
) -WorkingDirectory (Join-Path $ProjectRoot "backend") -WindowStyle Hidden
Write-Bootstrap "后端进程已请求启动：127.0.0.1:$BackendPort"

$ViteExe = Join-Path $ProjectRoot "frontend\node_modules\.bin\vite.cmd"
if (-not (Test-Path -LiteralPath $ViteExe)) {
    throw "前端依赖未安装，请先在 frontend 目录运行 pnpm install"
}
Start-Process -FilePath $ViteExe -ArgumentList @(
    "--host", "127.0.0.1", "--port", [string]$FrontendPort, "--strictPort"
) `
    -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden
Write-Bootstrap "前端进程已请求启动：127.0.0.1:$FrontendPort"

Write-Host "PersonalAgent 后端：http://127.0.0.1:$BackendPort/docs"
Write-Host "PersonalAgent 前端：http://127.0.0.1:$FrontendPort"
Write-Host "日志会话：$SessionId（停止后归档到 logs/archives）"
