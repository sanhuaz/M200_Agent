param(
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$TargetPorts = @(8000, 5176)
$LogRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "logs"))
$CurrentLogRoot = [System.IO.Path]::GetFullPath((Join-Path $LogRoot "current"))
$ArchiveLogRoot = [System.IO.Path]::GetFullPath((Join-Path $LogRoot "archives"))
$PythonExe = if ($PythonExe) { $PythonExe } else { $env:PERSONAL_AGENT_PYTHON }
$Resolver = Join-Path $PSScriptRoot "python-resolver.ps1"
$Interactive = -not [Console]::IsInputRedirected
$PythonExe = (& $Resolver -RequestedPath $PythonExe -Interactive:$Interactive | Select-Object -Last 1).Trim()

function Get-ProjectListeners {
    $netstatExe = Join-Path $env:WINDIR "System32\netstat.exe"
    if (-not (Test-Path -LiteralPath $netstatExe)) {
        throw "未找到 netstat.exe，无法检查项目监听端口。"
    }

    $netstatOutput = & $netstatExe -ano -p TCP
    if ($LASTEXITCODE -ne 0) {
        throw "netstat.exe 执行失败，无法检查项目监听端口。"
    }

    $listeners = foreach ($line in $netstatOutput) {
        if ($line -notmatch '^\s*TCP\s+\S+:(?<port>\d+)\s+\S+\s+LISTENING\s+(?<processId>\d+)\s*$') {
            continue
        }

        $localPort = [int]$Matches.port
        if ($localPort -in $TargetPorts) {
            [pscustomobject]@{
                LocalPort = $localPort
                OwningProcess = [int]$Matches.processId
            }
        }
    }

    return @(
        $listeners | Sort-Object LocalPort, OwningProcess -Unique
    )
}

function Test-PortArgument {
    param(
        [Parameter(Mandatory)]
        [string]$CommandLine,

        [Parameter(Mandatory)]
        [int]$Port
    )

    $portPattern = '(?i)(?:^|\s)--port(?:=|\s+)["'']?{0}(?:\s|$)' -f $Port
    return $CommandLine -match $portPattern
}

function Get-ProjectProcess {
    param(
        [Parameter(Mandatory)]
        [int]$ProcessId
    )

    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not $process) { return $null }
        return [pscustomobject]@{
            Name = [string]$process.Name
            ExecutablePath = [string]$process.ExecutablePath
            CommandLine = [string]$process.CommandLine
            ValidationMode = "cim"
        }
    } catch {
        $fallback = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $fallback) { return $null }
        $executablePath = $null
        try { $executablePath = [string]$fallback.Path } catch { }
        Write-Warning "无法读取 PID $ProcessId 的 WMI 命令行，改用端口、可执行路径和项目身份进行受限校验。"
        return [pscustomobject]@{
            Name = "$($fallback.ProcessName).exe"
            ExecutablePath = $executablePath
            CommandLine = $null
            ValidationMode = "limited"
        }
    }
}

function Test-BackendEndpointIdentity {
    param(
        [Parameter(Mandatory)]
        [string]$ExpectedPython
    )

    try {
        $root = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:8000/" -TimeoutSec 3
        $health = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 3
        if ($root.service -ne "personal-agent" -or -not $health.python_executable) {
            return $false
        }
        $actualPython = [System.IO.Path]::GetFullPath([string]$health.python_executable)
        return [string]::Equals(
            $actualPython,
            [System.IO.Path]::GetFullPath($ExpectedPython),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Test-FrontendEndpointIdentity {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5176/" -TimeoutSec 3
        $content = [string]$response.Content
        return (
            $content -match '(?i)<title>\s*PersonalAgent\s*</title>' -and
            $content -match '(?i)<div\s+id=["'']app["'']\s*></div>' -and
            $content -match '(?i)/src/main\.ts'
        )
    } catch {
        return $false
    }
}

function Test-BackendProcess {
    param(
        [Parameter(Mandatory)]
        [psobject]$Process
    )

    if (-not $Process.ExecutablePath -or -not $PythonExe) {
        return $false
    }

    $expectedPython = [System.IO.Path]::GetFullPath($PythonExe)
    $actualExecutable = [System.IO.Path]::GetFullPath($Process.ExecutablePath)
    if ($actualExecutable -ne $expectedPython) { return $false }
    if ($Process.CommandLine) {
        return (
            $Process.CommandLine -match "(?i)(?:^|\s)-m\s+uvicorn\s+app\.main:app(?:\s|$)" -and
            (Test-PortArgument -CommandLine $Process.CommandLine -Port 8000)
        )
    }
    return Test-BackendEndpointIdentity -ExpectedPython $expectedPython
}

function Test-FrontendProcess {
    param(
        [Parameter(Mandatory)]
        [psobject]$Process
    )

    if (-not $Process.ExecutablePath) {
        return $false
    }

    $frontendPath = [regex]::Escape((Join-Path $ProjectRoot "frontend"))
    $executableName = [System.IO.Path]::GetFileName($Process.ExecutablePath)
    if ($executableName -ine "node.exe") { return $false }
    if ($Process.CommandLine) {
        return (
            $Process.CommandLine -match "(?i)$frontendPath[\\/]" -and
            $Process.CommandLine -match "(?i)(?:^|[\\/])vite(?:\.cmd|\.js|\.mjs)?(?:\s|$|[\\/])" -and
            (Test-PortArgument -CommandLine $Process.CommandLine -Port 5176)
        )
    }
    return Test-FrontendEndpointIdentity
}

function Archive-CurrentLogs {
    New-Item -ItemType Directory -Path $ArchiveLogRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $CurrentLogRoot)) { return }
    $currentRootResolved = (Resolve-Path -LiteralPath $CurrentLogRoot).Path
    $directories = @(Get-ChildItem -LiteralPath $currentRootResolved -Directory -ErrorAction SilentlyContinue)
    foreach ($directory in $directories) {
        $sessionPath = [System.IO.Path]::GetFullPath($directory.FullName)
        if ((Split-Path -Parent $sessionPath) -ne $currentRootResolved) {
            throw "日志会话路径不在 logs/current 下，拒绝归档：$sessionPath"
        }
        $sessionId = $directory.Name -replace '[^A-Za-z0-9_-]', '_'
        $started = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
        $metadataPath = Join-Path $sessionPath "session.json"
        if (Test-Path -LiteralPath $metadataPath) {
            try {
                $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding utf8 | ConvertFrom-Json
                if ($metadata.started_at) {
                    $started = ([DateTime]::Parse([string]$metadata.started_at)).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
                }
                if ($metadata.session_id) { $sessionId = ([string]$metadata.session_id) -replace '[^A-Za-z0-9_-]', '_' }
            } catch {
                Write-Warning "无法读取 $metadataPath，使用目录名和当前时间生成归档名。"
            }
        }
        if (-not $sessionId) { $sessionId = "unknown" }
        $archivePath = Join-Path $ArchiveLogRoot "m200-agent-$started-$sessionId.zip"
        if (Test-Path -LiteralPath $archivePath) {
            $existing = Get-Item -LiteralPath $archivePath
            if ($existing.Length -gt 0) {
                Write-Host "日志归档已存在：$archivePath"
                Remove-Item -LiteralPath $sessionPath -Recurse -Force
                continue
            }
            Remove-Item -LiteralPath $archivePath -Force
        }
        $temporaryArchive = Join-Path $ArchiveLogRoot (".$sessionId." + [guid]::NewGuid().ToString("N") + ".tmp.zip")
        try {
            Compress-Archive -LiteralPath $sessionPath -DestinationPath $temporaryArchive -CompressionLevel Optimal -Force
            $created = Get-Item -LiteralPath $temporaryArchive -ErrorAction Stop
            if ($created.Length -le 0) { throw "ZIP 文件为空" }
            Move-Item -LiteralPath $temporaryArchive -Destination $archivePath -Force
            $final = Get-Item -LiteralPath $archivePath -ErrorAction Stop
            if ($final.Length -le 0) { throw "ZIP 文件校验失败" }
            Remove-Item -LiteralPath $sessionPath -Recurse -Force
            Write-Host "日志已归档：$archivePath"
        } catch {
            if (Test-Path -LiteralPath $temporaryArchive) { Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue }
            throw "日志归档失败，原始日志已保留：$sessionPath；$($_.Exception.Message)"
        }
    }
}

$listeners = Get-ProjectListeners
if ($listeners.Count -eq 0) {
    Write-Host "PersonalAgent 当前未运行，端口 8000、5176 均无监听。"
    Archive-CurrentLogs
    exit 0
}

$processesToStop = @{}
$validationErrors = @()

foreach ($listener in $listeners) {
    $processId = [int]$listener.OwningProcess
    $process = Get-ProjectProcess -ProcessId $processId
    if (-not $process) {
        continue
    }

    $isExpectedProcess = switch ([int]$listener.LocalPort) {
        8000 { Test-BackendProcess -Process $process }
        5176 { Test-FrontendProcess -Process $process }
        default { $false }
    }

    if (-not $isExpectedProcess) {
        $validationErrors += "端口 $($listener.LocalPort) 的 PID $processId（$($process.Name)）不是可确认的 PersonalAgent 进程"
        continue
    }

    $processesToStop[$processId] = $process.Name
}

if ($validationErrors.Count -gt 0) {
    $details = $validationErrors -join [Environment]::NewLine
    throw "为避免误杀，未停止任何进程：$([Environment]::NewLine)$details"
}

try {
    Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:8000/api/v1/logs/finalize" -TimeoutSec 3 | Out-Null
    Write-Host "已通知后端刷新停止日志。"
} catch {
    Write-Warning "未能通知后端刷新停止日志，将继续执行安全停止：$($_.Exception.Message)"
}

foreach ($processId in $processesToStop.Keys) {
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $processId -ErrorAction Stop
        Write-Host "已停止 PersonalAgent 进程：PID $processId（$($processesToStop[$processId])）"
    }
}

foreach ($processId in $processesToStop.Keys) {
    Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue
}

$remainingListeners = Get-ProjectListeners
if ($remainingListeners.Count -gt 0) {
    $details = $remainingListeners |
        ForEach-Object { "端口 $($_.LocalPort)，PID $($_.OwningProcess)" }
    throw "PersonalAgent 停止后仍有目标端口处于监听状态：$($details -join '；')"
}

Write-Host "PersonalAgent 已停止，端口 8000、5176 均已释放。"
Archive-CurrentLogs
