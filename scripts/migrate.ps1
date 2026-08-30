param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DatabasePath = Join-Path $ProjectRoot "data\personal_agent.db"
$OriginalLocation = Get-Location
Set-Location -LiteralPath $ProjectRoot
try {

function Backup-RuntimeData([string]$Label) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupRoot = Join-Path $ProjectRoot ("_v020_backups\" + $Label + "-" + $stamp)
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    foreach ($relative in @("data\personal_agent.db", "data\langgraph_checkpoints.db", "data\chroma", "data\uploads", "data\downloads")) {
        $source = Join-Path $ProjectRoot $relative
        if (Test-Path -LiteralPath $source) {
            $destination = Join-Path $backupRoot $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force | Out-Null
        }
    }
    @{
        format_version = 1
        kind = "automatic_migration"
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        label = $Label
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot "backup.json") -Encoding utf8
    return $backupRoot
}

function Remove-ExpiredAutomaticBackups([int]$Keep = 3) {
    $backupBase = Join-Path $ProjectRoot "_v020_backups"
    if (-not (Test-Path -LiteralPath $backupBase)) { return }
    $automatic = @(
        Get-ChildItem -LiteralPath $backupBase -Directory -Force | Where-Object {
            $manifestPath = Join-Path $_.FullName "backup.json"
            if (-not (Test-Path -LiteralPath $manifestPath)) { return $false }
            try {
                $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
                return $manifest.kind -eq "automatic_migration"
            } catch {
                return $false
            }
        } | Sort-Object LastWriteTime -Descending
    )
    foreach ($directory in ($automatic | Select-Object -Skip $Keep)) {
        $resolved = [System.IO.Path]::GetFullPath($directory.FullName)
        $expectedParent = [System.IO.Path]::GetFullPath($backupBase)
        if ((Split-Path -Parent $resolved) -ne $expectedParent) {
            throw "自动备份路径越界，拒绝清理：$resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "已按保留策略清理自动迁移备份：$resolved"
    }
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python 不存在：$PythonExe"
}

$planJson = & $PythonExe (Join-Path $PSScriptRoot "migration_plan.py") `
    --database $DatabasePath --config (Join-Path $ProjectRoot "alembic.ini")
if ($LASTEXITCODE -ne 0) { throw "无法读取数据库迁移状态" }
$plan = $planJson | ConvertFrom-Json
$backupCreated = $false

if ($plan.needs_stamp) {
    $backupRoot = Backup-RuntimeData "migration"
    $backupCreated = $true
    & $PythonExe -m alembic stamp 0001_initial
    if ($LASTEXITCODE -ne 0) { throw "旧数据库基线标记失败，已保留备份：$backupRoot" }
    Write-Host "旧数据库已标记为 v0.1 基线，备份：$backupRoot"
}

if ($plan.needs_backup -and -not $plan.needs_stamp) {
    $backupRoot = Backup-RuntimeData "migration"
    $backupCreated = $true
    Write-Host "迁移前已备份运行数据：$backupRoot"
}

    & $PythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，后端不会启动" }
    if ($backupCreated) { Remove-ExpiredAutomaticBackups }
    Write-Host "数据库迁移完成"
} finally {
    Set-Location -LiteralPath $OriginalLocation
}
