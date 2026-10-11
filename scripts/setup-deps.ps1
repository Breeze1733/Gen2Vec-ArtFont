<#
.SYNOPSIS
    One-shot bootstrap for every local development dependency.
.DESCRIPTION
    Prompts for and runs each step in order:
      1. download-comfyui-engine.ps1   - download the ComfyUI portable bundle and custom nodes
      2. configure-comfyui.ps1         - configure ComfyUI (patches, Python packages)
      3. download-models.ps1           - download AI models into ComfyUI models/
      4. download-isnet-general-use.ps1 - download the rembg model used by the vectorizer

    Each step asks for confirmation (y/n); answer n to skip it.
.NOTES
    Intended for a freshly cloned repo; also useful for resetting dependencies.
    Every script is located relative to the repo root, so do not move this file.
#>

$ErrorActionPreference = 'Stop'

# -- Locate the repository root ---------------------------------
$ScriptDir = $PSScriptRoot
$RepoRoot  = Resolve-Path (Join-Path $ScriptDir '..')

# -- Step definitions -------------------------------------------
$Steps = @(
    @{
        Name        = 'Download the ComfyUI engine and custom nodes'
        Description = 'scripts/download-comfyui-engine.ps1'
        WorkDir     = Join-Path $RepoRoot 'services\txt2img-api'
        ScriptRel   = 'scripts\download-comfyui-engine.ps1'
    }
    @{
        Name        = 'Configure ComfyUI (patches / Python packages)'
        Description = 'scripts/configure-comfyui.ps1'
        WorkDir     = Join-Path $RepoRoot 'services\txt2img-api'
        ScriptRel   = 'scripts\configure-comfyui.ps1'
    }
    @{
        Name        = 'Download AI models into ComfyUI models/'
        Description = 'scripts/download-models.ps1'
        WorkDir     = Join-Path $RepoRoot 'services\txt2img-api'
        ScriptRel   = 'scripts\download-models.ps1'
    }
    @{
        Name        = 'Download the rembg model for the vectorizer'
        Description = 'services\vectorizer-api\models\rembg\download-isnet-general-use.ps1'
        WorkDir     = $RepoRoot
        ScriptRel   = 'services\vectorizer-api\models\rembg\download-isnet-general-use.ps1'
    }
)

Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host '   Gen2Vec ArtFont - development dependency bootstrap' -ForegroundColor Cyan
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host ''

foreach ($Step in $Steps) {
    $fullPath = Resolve-Path (Join-Path $RepoRoot $Step.ScriptRel)

    Write-Host "-> Step: $($Step.Name)" -ForegroundColor Yellow
    Write-Host "   $($Step.Description)" -ForegroundColor DarkGray

    do {
        $answer = Read-Host '   Continue? (y/n)'
        $answer = $answer.Trim().ToLower()
    } while ($answer -notin @('y', 'n'))

    if ($answer -eq 'n') {
        Write-Host '   Skipped.' -ForegroundColor Gray
        Write-Host ''
        continue
    }

    Write-Host '   Running, please wait...' -ForegroundColor Green
    Write-Host ''

    Push-Location $Step.WorkDir
    try {
        & $fullPath
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            Write-Host "   [FAIL] script exit code $LASTEXITCODE" -ForegroundColor Red
            Write-Host '   Continuing to the next step.' -ForegroundColor Gray
        } else {
            Write-Host "   [OK] done" -ForegroundColor Green
        }
    } catch {
        Write-Host "   [FAIL] exception: $_" -ForegroundColor Red
        Write-Host '   Continuing to the next step.' -ForegroundColor Gray
    } finally {
        Pop-Location
    }
    Write-Host ''
}

Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host '   All steps finished.' -ForegroundColor Cyan
Write-Host '   If a step was skipped, run its script separately.' -ForegroundColor Cyan
Write-Host '=====================================================' -ForegroundColor Cyan
