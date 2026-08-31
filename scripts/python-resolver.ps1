param(
    [string]$RequestedPath,
    [switch]$Interactive,
    [string[]]$RequiredModules = @()
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
        [string]$Candidate,

        [string[]]$Modules = @()
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
    $normalizedModules = @(
        $Modules |
            Where-Object { $_ } |
            ForEach-Object { ([string]$_).Trim() } |
            Sort-Object -Unique
    )
    foreach ($module in $normalizedModules) {
        if ($module -notmatch '^[A-Za-z_][A-Za-z0-9_.]*$') {
            throw "依赖模块名无效：$module"
        }
    }
    if ($normalizedModules.Count -gt 0) {
        $moduleList = $normalizedModules -join ","
        $missingProbe = @(
            & $resolved -c (
                "import importlib.util,sys; modules=sys.argv[1].split(',') if sys.argv[1] else []; " +
                "print(','.join(module for module in modules if importlib.util.find_spec(module) is None))"
            ) $moduleList 2>$null
        )
        if ($LASTEXITCODE -ne 0 -or $missingProbe.Count -lt 1) {
            throw "无法检查项目依赖模块"
        }
        $missing = ([string]$missingProbe[-1]).Trim()
        if ($missing) {
            throw "缺少项目依赖模块：$missing"
        }
    }
    return $reported
}

function Resolve-PersonalAgentPython {
    param(
        [string]$ExplicitPath,
        [switch]$AllowInteractive,
        [string[]]$Modules = @()
    )

    $explicit = $ExplicitPath
    if (-not $explicit) { $explicit = $env:PERSONAL_AGENT_PYTHON }
    if ($explicit) {
        try {
            return Resolve-PythonPath -Candidate $explicit -Modules $Modules
        } catch {
            $requirements = Join-Path $ProjectRoot "requirements.txt"
            throw "指定的 Python 无效（$explicit）：$($_.Exception.Message)。请改用 -RequestedPath 或 PERSONAL_AGENT_PYTHON 指定已安装项目依赖的 Python 3.13，或用该解释器运行 -m pip install -r `"$requirements`"。"
        }
    }

    $venv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv -PathType Leaf) {
        try { return Resolve-PythonPath -Candidate $venv -Modules $Modules } catch {
            Write-Warning "跳过项目 .venv：$($_.Exception.Message)"
        }
    }

    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $probe = @(& $launcher.Source -3.13 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $probe.Count -ge 1) {
                return Resolve-PythonPath -Candidate ([string]$probe[0]).Trim() -Modules $Modules
            }
        } catch {
            Write-Warning "跳过 py -3.13：$($_.Exception.Message)"
        }
    }

    $pathCommands = @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($command in $pathCommands) {
        try { return Resolve-PythonPath -Candidate $command.Source -Modules $Modules } catch {
            Write-Warning "跳过 PATH Python（$($command.Source)）：$($_.Exception.Message)"
        }
    }

    if ($AllowInteractive) {
        while ($true) {
            $manual = Read-Host "未找到满足版本和依赖要求的 Python 3.13，请输入 python.exe 的完整路径（留空退出）"
            if (-not $manual) { break }
            try { return Resolve-PythonPath -Candidate $manual.Trim('"') -Modules $Modules } catch {
                Write-Warning "Python 路径不可用：$($_.Exception.Message)"
            }
        }
    }

    $requirements = Join-Path $ProjectRoot "requirements.txt"
    throw "未找到满足版本和项目依赖要求的 Python 3.13。请设置 PERSONAL_AGENT_PYTHON，或使用 -PythonExe/-RequestedPath 指定解释器；如尚未安装依赖，请用目标解释器运行 -m pip install -r `"$requirements`"。"
}

Resolve-PersonalAgentPython -ExplicitPath $RequestedPath -AllowInteractive:$Interactive -Modules $RequiredModules
