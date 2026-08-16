[CmdletBinding()]
param(
    [switch]$Watch
)

$cli = Join-Path $PSScriptRoot "tailwindcss.exe"
$input = Join-Path $PSScriptRoot "input.css"
$output = Join-Path $PSScriptRoot "frontend/styles.css"

if (-not (Test-Path $cli)) {
    Write-Error "Không tìm thấy tailwindcss.exe tại $cli"
    exit 1
}

if ($Watch) {
    Write-Host "Đang chạy Tailwind CSS ở chế độ Watch... (Nhấn Ctrl+C để dừng)" -ForegroundColor Cyan
    & $cli -i $input -o $output --watch
} else {
    Write-Host "Đang biên dịch và nén Tailwind CSS..." -ForegroundColor Green
    & $cli -i $input -o $output --minify
}
