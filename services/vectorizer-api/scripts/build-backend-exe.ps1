<#
.SYNOPSIS
  Build a single-file `vectorizer-backend.exe` for the vectorizer-api service.

.DESCRIPTION
  Mirrors `services/txt2img-api/scripts/build-backend-exe.ps1` with shared
  structure.  Supports two toolchains:

    uv (default) - runs `uv sync` to create/refresh this service's own
                   .venv from pyproject.toml + uv.lock, then builds with
                   `uv run python -m PyInstaller`.  Requires pyproject.toml.
    python       - uses the system/global Python; use this if you manage
                   your own venv manually or uv is not installed.

.PARAMETER Toolchain
  Either "uv" (default) or "python".  See description above.

.EXAMPLE
  .\scripts\build-backend-exe.ps1
  .\scripts\build-backend-exe.ps1 -Toolchain python
#>
param(
  [ValidateSet("uv", "python")]
  [string]$Toolchain = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Native command stderr must not be treated as fatal (it raises NativeCommandError when output is redirected)
function Invoke-Tool {
  param([string]$Command, [string[]]$Arguments)
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $Command @Arguments | Out-Host
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previous
  }
}

# Auto-detect: prefer uv, fall back to python
if (-not $Toolchain) {
  if (Get-Command uv -ErrorAction SilentlyContinue) {
    $Toolchain = "uv"
    Write-Host "auto-detect: uv found, using uv"
  } else {
    $Toolchain = "python"
    Write-Host "auto-detect: uv not found, falling back to python"
  }
}

if ($Toolchain -eq "uv") {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH. Install it (https://docs.astral.sh/uv/) or re-run with -Toolchain python."
  }
  if (-not (Test-Path "pyproject.toml")) {
    throw "pyproject.toml not found in $root. The uv toolchain needs a uv-managed project; re-run with -Toolchain python."
  }
  $installArgs = @("sync")
  $pyRunArgs = @("run", "python", "-m", "PyInstaller")
} else {
  $installArgs = @("-m", "pip", "install", "--upgrade", "pip", "pyinstaller")
  $pyRunArgs = @("-m", "PyInstaller")
}

Write-Host "[1/5] Preparing build environment via $Toolchain..."
$exitCode = Invoke-Tool $Toolchain $installArgs
if ($exitCode -ne 0) {
  throw "$Toolchain $($installArgs -join ' ') failed with exit code $exitCode"
}

Write-Host "[2/5] Cleaning previous build outputs..."
# Only remove PyInstaller artifacts; preserve other dist/ files.
if (Test-Path "build") { Remove-Item -LiteralPath "build" -Recurse -Force }
if (Test-Path "vectorizer-backend.spec") {
  Remove-Item -LiteralPath "vectorizer-backend.spec" -Force
}
$oldExe = Join-Path "dist" "vectorizer-backend.exe"
if (Test-Path $oldExe) { Remove-Item -LiteralPath $oldExe -Force }

Write-Host "[3/5] Building vectorizer-backend.exe with PyInstaller..."
$exitCode = Invoke-Tool $Toolchain ($pyRunArgs + @(
  "--noconfirm"
  "--clean"
  "--name", "vectorizer-backend"
  "--onefile"
  "--console"
  "--collect-all", "skimage"
  "--collect-all", "resvg_py"
  "--collect-all", "cv2"
  "--collect-all", "rembg"
  "--collect-all", "onnxruntime"
  "--collect-all", "jsonschema"
  "--collect-all", "jsonschema_specifications"
  "--collect-all", "referencing"
  "--collect-all", "rpds"
  "--collect-all", "lark"
  "--collect-all", "rfc3987_syntax"
  "--collect-all", "vtracer"
  "--hidden-import", "uvicorn.logging"
  "--hidden-import", "uvicorn.loops.auto"
  "--hidden-import", "uvicorn.protocols.http.auto"
  "--hidden-import", "uvicorn.protocols.websockets.auto"
  "--hidden-import", "uvicorn.lifespan.on"
  "backend_entry.py"
))
if ($exitCode -ne 0) {
  throw "PyInstaller failed with exit code $exitCode"
}

Write-Host "[4/5] Preparing rembg model and copying distribution files to dist/..."

$modelDir = Join-Path $root "models\rembg"
$modelFile = Join-Path $modelDir "isnet-general-use.onnx"
if (-not (Test-Path $modelFile)) {
  $downloadScript = Join-Path $modelDir "download-isnet-general-use.ps1"
  if (-not (Test-Path $downloadScript)) {
    throw "rembg model missing and download script not found: $downloadScript"
  }
  Write-Host "  rembg model not found, downloading..."
  $exitCode = Invoke-Tool $downloadScript @()
  if ($exitCode -ne 0) {
    throw "Failed to download rembg model (exit code $exitCode)."
  }
  if (-not (Test-Path $modelFile)) {
    throw "rembg model still missing after download: $modelFile"
  }
}

New-Item -ItemType Directory -Path "dist\models" -Force | Out-Null
Copy-Item -Path "models\*" -Destination "dist\models" -Recurse -Force
Write-Host "  OK  models/"

Write-Host "[5/5] Done."
Write-Host ""
Write-Host "EXE output: $root\dist\vectorizer-backend.exe"
Write-Host ""
Write-Host "Distribution layout:"
Get-ChildItem (Join-Path $root "dist") | ForEach-Object {
  $size = if ($_.Length -gt 1GB) {
    "{0:N1} GB" -f ($_.Length / 1GB)
  } elseif ($_.Length -gt 1MB) {
    "{0:N0} MB" -f ($_.Length / 1MB)
  } else {
    "{0:N0} KB" -f ($_.Length / 1KB)
  }
  Write-Host "    $($_.Name)  ($size)"
}
Write-Host ""
Write-Host "The dist/ directory is the final distribution package."
