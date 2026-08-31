param(
    [string]$RequestedPath,
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Get-PythonVersionProbe {
    param(
        [Parameter(Mandatory)]
        [string]$Executable
    )

    $probe = @(& $Executable -c "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $probe.Count -lt 2) {
        throw "无法读取 Python 解释器信息"
    }
    [pscustomobject]@{
        Executable = ([string]$probe[0]).Trim()
        Version = ([string]$probe[1]).Trim()
    }
}

function Resolve-PythonPath {
    param(
        [Parameter(Mandatory)]
        [string]$Candidate
    )

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw "文件不存在"
    }
    $resolved = (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    $probe = Get-PythonVersionProbe -Executable $resolved
    if ($probe.Version -ne "3.13") {
        throw "实际版本为 $($probe.Version)，需要 Python 3.13"
    }
    if (-not (Test-Path -LiteralPath $probe.Executable -PathType Leaf)) {
        throw "sys.executable 不存在：$($probe.Executable)"
    }
    $reported = (Resolve-Path -LiteralPath $probe.Executable -ErrorAction Stop).Path
    if (-not [string]::Equals($reported, $resolved, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "sys.executable 与候选路径不一致：$($probe.Executable)"
    }
    return $reported
}

function Resolve-PersonalAgentPython {
    param(
        [string]$ExplicitPath,
        [switch]$AllowInteractive
    )

    $explicit = $ExplicitPath
    if (-not $explicit) { $explicit = $env:PERSONAL_AGENT_PYTHON }
    if ($explicit) {
        try {
            return Resolve-PythonPath -Candidate $explicit
        } catch {
            throw "指定的 Python 无效（$explicit）：$($_.Exception.Message)。请改用 -RequestedPath 或 PERSONAL_AGENT_PYTHON 指定 Python 3.13。"
        }
    }

    $venv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv -PathType Leaf) {
        try { return Resolve-PythonPath -Candidate $venv } catch { }
    }

    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $probe = @(& $launcher.Source -3.13 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $probe.Count -ge 1) {
                return Resolve-PythonPath -Candidate ([string]$probe[0]).Trim()
            }
        } catch { }
    }

    $pathCommands = @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($command in $pathCommands) {
        try { return Resolve-PythonPath -Candidate $command.Source } catch { }
    }

    if ($AllowInteractive) {
        while ($true) {
            $manual = Read-Host "未找到 Python 3.13，请输入 python.exe 的完整路径（留空退出）"
            if (-not $manual) { break }
            try { return Resolve-PythonPath -Candidate $manual.Trim('"') } catch {
                Write-Warning "Python 路径不可用：$($_.Exception.Message)"
            }
        }
    }

    throw "未找到可用的 Python 3.13。请设置 PERSONAL_AGENT_PYTHON，或使用 -PythonExe/ -RequestedPath 指定解释器。"
}

Resolve-PersonalAgentPython -ExplicitPath $RequestedPath -AllowInteractive:$Interactive
