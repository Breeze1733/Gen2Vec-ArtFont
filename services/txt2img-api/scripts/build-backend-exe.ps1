<#
.SYNOPSIS
  Build a single-file `txt2img-backend.exe` for the txt2img-api service.

.DESCRIPTION
  Mirrors `services/vectorizer-api/scripts/build-backend-exe.ps1`, but is
  tailored to the txt2img-api layout:

    * Workflow JSON files are bundled via `--add-data`.
    * The ComfyUI portable bundle (~60 GB) is intentionally NOT bundled
      and must live next to the EXE at runtime. See README.
    * `torch` is NOT a dependency of this service (ComfyUI provides it).
    * Uses `uv` (rather than bare `python`) so the build runs against the
      service's own `.venv` even when a different venv is on PATH.

.PARAMETER Toolchain
  Either "uv" (default) or "python". "uv" is recommended because it always
  resolves to the txt2img-api project's .venv and brings its own
  pip/PyInstaller. Use "python" only if you maintain a global venv.

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
  # `uv sync` creates the project's own .venv and installs the runtime plus the
  # dev-group dependencies (pyinstaller lives there). The previous `uv pip install`
  # required an already-existing venv and never installed the runtime deps, so it
  # failed outright on a clean checkout. Mirrors the vectorizer build script.
  $installArgs = @("sync")
  $pyRunArgs = @("run", "python", "-m", "PyInstaller")
} else {
  $installArgs = @("-m", "pip", "install", "--upgrade", "pip", "pyinstaller")
  $pyRunArgs = @("-m", "PyInstaller")
}

Write-Host "[1/4] Preparing build environment via $Toolchain..."
$exitCode = Invoke-Tool $Toolchain $installArgs
if ($exitCode -ne 0) {
  throw "$Toolchain $($installArgs -join ' ') failed with exit code $exitCode"
}

Write-Host "[2/4] Cleaning previous build outputs..."
# Only remove PyInstaller artifacts; preserve other dist/ files (e.g. ComfyUI-Engine.exe).
if (Test-Path "build") { Remove-Item -LiteralPath "build" -Recurse -Force }
if (Test-Path "txt2img-backend.spec") {
  Remove-Item -LiteralPath "txt2img-backend.spec" -Force
}
# Remove old EXE so PyInstaller does a clean rebuild.
$oldExe = Join-Path "dist" "txt2img-backend.exe"
if (Test-Path $oldExe) { Remove-Item -LiteralPath $oldExe -Force }

# Resolve the workflows directory (create it if a file is missing so
# PyInstaller does not fail the `--add-data` glob at build time).
$workflowsDir = Join-Path $root "workflows"
if (-not (Test-Path $workflowsDir)) {
  New-Item -ItemType Directory -Path $workflowsDir | Out-Null
}
$workflowFiles = Get-ChildItem -Path $workflowsDir -Filter "*.json" -ErrorAction SilentlyContinue
$addDataArgs = @()
foreach ($f in $workflowFiles) {
  $addDataArgs += "--add-data"
  # Windows uses ';' as the separator between source and dest in --add-data.
  $addDataArgs += "workflows\$($f.Name);workflows"
}

Write-Host "[3/4] Building txt2img-backend.exe with PyInstaller..."
$pyinstallerArgs = @(
  "--noconfirm"
  "--clean"
  "--name", "txt2img-backend"
  "--onefile"
  "--console"
  "--paths", "app"
) + $addDataArgs + @(
  "--collect-all", "fastapi"
  "--collect-all", "uvicorn"
  "--collect-all", "httpx"
  "--collect-all", "PIL"
  "--collect-all", "anyio"
  "--hidden-import", "uvicorn.logging"
  "--hidden-import", "uvicorn.loops.auto"
  "--hidden-import", "uvicorn.protocols.http.auto"
  "--hidden-import", "uvicorn.protocols.websockets.auto"
  "--hidden-import", "uvicorn.lifespan.on"
  "txt2img_entry.py"
)

$exitCode = Invoke-Tool $Toolchain ($pyRunArgs + $pyinstallerArgs)
if ($exitCode -ne 0) {
  throw "PyInstaller failed with exit code $exitCode"
}

Write-Host "[4/4] Done."
Write-Host ""

$distDir = Join-Path $root "dist"
Write-Host "EXE output: $distDir\txt2img-backend.exe"
Write-Host ""
Write-Host "Distribution layout (place ComfyUI-Engine.exe in dist/ to complete):"
Get-ChildItem $distDir | ForEach-Object {
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
Write-Host "The dist/ directory is the final distribution package. Distribute all files in it to end users."
