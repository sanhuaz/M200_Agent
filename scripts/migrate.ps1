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
    return $backupRoot
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python 不存在：$PythonExe"
}

$databaseLiteral = $DatabasePath.Replace("'", "''")
$state = & $PythonExe -c "import sqlite3; p=r'$databaseLiteral'; c=sqlite3.connect(p); names={r[0] for r in c.execute('select name from sqlite_master')}; print('empty' if not names else 'legacy' if 'alembic_version' not in names else 'versioned'); c.close()"
if ($LASTEXITCODE -ne 0) { throw "无法读取数据库迁移状态" }

if ($state -eq "legacy") {
    $backupRoot = Backup-RuntimeData "migration"
    & $PythonExe -m alembic stamp 0001_initial
    if ($LASTEXITCODE -ne 0) { throw "旧数据库基线标记失败，已保留备份：$backupRoot" }
    Write-Host "旧数据库已标记为 v0.1 基线，备份：$backupRoot"
}

if ($state -eq "versioned") {
    $version = & $PythonExe -c "import sqlite3; c=sqlite3.connect(r'$databaseLiteral'); print(c.execute('select version_num from alembic_version').fetchone()[0]); c.close()"
    if ($LASTEXITCODE -ne 0) { throw "无法读取当前迁移版本" }
    if ($version -ne "0002_v020") {
        $backupRoot = Backup-RuntimeData "migration"
        Write-Host "迁移前已备份运行数据：$backupRoot"
    }
}

    & $PythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，后端不会启动" }
    Write-Host "数据库迁移完成"
} finally {
    Set-Location -LiteralPath $OriginalLocation
}
