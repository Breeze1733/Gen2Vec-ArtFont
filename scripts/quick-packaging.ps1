$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# Build the txt2img backend EXE
Write-Host "=== [1/4] Building txt2img-backend.exe ==="
Push-Location "$root\services\txt2img-api"
& "$root\services\txt2img-api\scripts\build-backend-exe.ps1"
Pop-Location

# Build the vectorizer backend EXE
Write-Host "=== [2/4] Building vectorizer-backend.exe ==="
Push-Location "$root\services\vectorizer-api"
& "$root\services\vectorizer-api\scripts\build-backend-exe.ps1"
Pop-Location

# Build the single-file CLI EXE
Write-Host "=== [3/4] Building gen2vec_cli.exe ==="
Push-Location "$root\apps\cli"
& "$root\apps\cli\scripts\build-cli-exe.ps1"
Pop-Location

# Build the desktop installer
Write-Host "=== [4/4] Building desktop installer ==="
Push-Location "$root\apps\desktop"
npm run electron:build
Pop-Location
