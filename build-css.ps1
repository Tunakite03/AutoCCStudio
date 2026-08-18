[CmdletBinding()]
param(
    [switch]$Watch
)

$cli = Join-Path $PSScriptRoot "tailwindcss.exe"
# Not $input/$output: $input is a PowerShell automatic variable (the pipeline
# enumerator) and gets re-bound inside an advanced script's end block, so the
# path assigned here would never reach the CLI.
$inputFile = Join-Path $PSScriptRoot "frontend/styles/input.css"
$outputFile = Join-Path $PSScriptRoot "frontend/styles.css"

if (-not (Test-Path $cli)) {
    Write-Error "Không tìm thấy tailwindcss.exe tại $cli"
    exit 1
}

if (-not (Test-Path $inputFile)) {
    Write-Error "Không tìm thấy file nguồn CSS tại $inputFile"
    exit 1
}

if ($Watch) {
    Write-Host "Đang chạy Tailwind CSS ở chế độ Watch... (Nhấn Ctrl+C để dừng)" -ForegroundColor Cyan
    & $cli -i $inputFile -o $outputFile --watch
} else {
    Write-Host "Đang biên dịch và nén Tailwind CSS..." -ForegroundColor Green
    & $cli -i $inputFile -o $outputFile --minify
}
