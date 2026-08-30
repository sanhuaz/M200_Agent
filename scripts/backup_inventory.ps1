param(
    [switch]$IncludeHashes
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$BackupRoot = Join-Path $ProjectRoot "_v020_backups"

if (-not (Test-Path -LiteralPath $BackupRoot)) {
    Write-Host "备份目录不存在：$BackupRoot"
    exit 0
}

$rows = foreach ($directory in Get-ChildItem -LiteralPath $BackupRoot -Directory -Force) {
    $files = @(Get-ChildItem -LiteralPath $directory.FullName -File -Recurse -Force)
    $manifestPath = Join-Path $directory.FullName "backup.json"
    $kind = "legacy_or_manual"
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
            if ($manifest.kind) { $kind = [string]$manifest.kind }
        } catch {
            $kind = "invalid_manifest"
        }
    }
    $hashes = $null
    if ($IncludeHashes) {
        $hashes = @(
            $files | ForEach-Object {
                [pscustomobject]@{
                    path = [System.IO.Path]::GetRelativePath($directory.FullName, $_.FullName)
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        )
    }
    [pscustomobject]@{
        name = $directory.Name
        kind = $kind
        last_write_time = $directory.LastWriteTime
        file_count = $files.Count
        size_mb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 2)
        hashes = $hashes
    }
}

$rows | Sort-Object last_write_time -Descending
