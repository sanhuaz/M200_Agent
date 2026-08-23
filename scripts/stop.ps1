$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$TargetPorts = @(8000, 5176)
$FixedPython = "D:\miniconda\envs\langchain1.2\python.exe"
$PythonExe = $env:PERSONAL_AGENT_PYTHON

if (-not $PythonExe) {
    $PythonExe = $FixedPython
}
if (Test-Path -LiteralPath $PythonExe) {
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
}

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

function Test-BackendProcess {
    param(
        [Parameter(Mandatory)]
        [CimInstance]$Process
    )

    if (-not $Process.ExecutablePath -or -not $Process.CommandLine -or -not $PythonExe) {
        return $false
    }

    $expectedPython = [System.IO.Path]::GetFullPath($PythonExe)
    $actualExecutable = [System.IO.Path]::GetFullPath($Process.ExecutablePath)
    return (
        $actualExecutable -eq $expectedPython -and
        $Process.CommandLine -match "(?i)(?:^|\s)-m\s+uvicorn\s+app\.main:app(?:\s|$)" -and
        (Test-PortArgument -CommandLine $Process.CommandLine -Port 8000)
    )
}

function Test-FrontendProcess {
    param(
        [Parameter(Mandatory)]
        [CimInstance]$Process
    )

    if (-not $Process.ExecutablePath -or -not $Process.CommandLine) {
        return $false
    }

    $frontendPath = [regex]::Escape((Join-Path $ProjectRoot "frontend"))
    $executableName = [System.IO.Path]::GetFileName($Process.ExecutablePath)
    return (
        $executableName -ieq "node.exe" -and
        $Process.CommandLine -match "(?i)$frontendPath[\\/]" -and
        $Process.CommandLine -match "(?i)(?:^|[\\/])vite(?:\.cmd|\.js|\.mjs)?(?:\s|$|[\\/])" -and
        (Test-PortArgument -CommandLine $Process.CommandLine -Port 5176)
    )
}

$listeners = Get-ProjectListeners
if ($listeners.Count -eq 0) {
    Write-Host "PersonalAgent 当前未运行，端口 8000、5176 均无监听。"
    exit 0
}

$processesToStop = @{}
$validationErrors = @()

foreach ($listener in $listeners) {
    $processId = [int]$listener.OwningProcess
    $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $processId"
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
