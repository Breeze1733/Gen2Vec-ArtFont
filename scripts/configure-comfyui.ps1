# configure-comfyui.ps1 - ComfyUI post-install configuration.
<#
.SYNOPSIS
Applies patched custom-node files and installs required Python packages
into a ComfyUI Windows portable installation.

Call this AFTER download-comfyui-engine.ps1 has finished.  The engine
download script already calls this script as its final step (step 6).

Patched custom-node sources are copied verbatim from disk (no embedded copies):
    custom-node-patches/comfyui-inspyrenet-rembg/                 next to this script (packaged)
    services/txt2img-api/custom_nodes/comfyui-inspyrenet-rembg/   repo checkout
Edit the source files directly; this script hashes them and re-applies
whenever the hash differs from the last applied one.

.PARAMETER DestDir
Target backend directory (the folder that contains
ComfyUI_windows_portable_nvidia). Defaults to the script directory.

.PARAMETER Electron
Electron integration mode: prints COMFYCFG: structured progress lines.

.EXAMPLE
.\configure-comfyui.ps1
.\configure-comfyui.ps1 -DestDir ..\backend
.\configure-comfyui.ps1 -Electron -DestDir .\backend\
#>

param(
    [string]$DestDir = $PSScriptRoot,
    [switch]$Electron
)

$ErrorActionPreference = "Stop"
$PREFIX = if ($Electron) { "COMFYCFG:" } else { "" }

# -- paths --
$ComfyPortable   = Join-Path $DestDir "ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable"
$ComfyMain       = Join-Path $ComfyPortable "ComfyUI\main.py"
$CustomNodesDir  = Join-Path $ComfyPortable "ComfyUI\custom_nodes"
$PythonExe       = Join-Path $ComfyPortable "python_embeded\python.exe"
$InspyNodeDir    = Join-Path $CustomNodesDir "comfyui-inspyrenet-rembg"
$PatchSentinel   = Join-Path $InspyNodeDir ".patches-applied"
$PipSentinel     = Join-Path $ComfyPortable ".pip-complete"

# -- helper functions --
function Emit {
    param([string]$Type, [string]$Message = "")
    if ($Electron) {
        if ($Message) { Write-Output "${PREFIX}${Type}|${Message}" }
        else { Write-Output "${PREFIX}${Type}" }
    }
}

function Write-Color {
    param([string]$Text, [string]$Color = "White")
    if (-not $Electron) { Write-Host $Text -ForegroundColor $Color }
}

function Write-Step {
    param([string]$Text)
    Write-Color "  $Text" DarkGray
}

function Finish-Ok {
    param([string]$Name, [string]$Detail = "")
    $msg = if ($Detail) { "${Name}|${Detail}" } else { $Name }
    Emit "DONE" $msg
    Write-Color "OK $Name $Detail" Green
}

function Finish-Skip {
    param([string]$Name, [string]$Detail = "")
    $msg = if ($Detail) { "${Name}|${Detail}" } else { $Name }
    Emit "SKIP" $msg
    Write-Color "Skip $Name $Detail" DarkGray
}

function Test-NonEmptyFile {
    param(
        [string]$Path,
        [long]$MinBytes = 1
    )
    if (-not (Test-Path $Path)) { return $false }
    try {
        $item = Get-Item -LiteralPath $Path
        return $item.PSIsContainer -eq $false -and $item.Length -ge $MinBytes
    } catch {
        return $false
    }
}

function Resolve-PatchSourceDir {
    <#
      Locate the patched custom-node source directory, in priority order:
        1. <DestDir>\custom-node-patches\comfyui-inspyrenet-rembg   packaged layout
        2. <script dir>\custom-node-patches\comfyui-inspyrenet-rembg
        3. <script dir>\..\services\txt2img-api\custom_nodes\comfyui-inspyrenet-rembg
      Returns $null when none of the candidates holds both patch files.
    #>
    param([string]$BaseDir)

    $candidates = @(
        (Join-Path $BaseDir     "custom-node-patches\comfyui-inspyrenet-rembg"),
        (Join-Path $PSScriptRoot "custom-node-patches\comfyui-inspyrenet-rembg"),
        (Join-Path $PSScriptRoot "..\services\txt2img-api\custom_nodes\comfyui-inspyrenet-rembg")
    )

    foreach ($candidate in $candidates) {
        if ((Test-NonEmptyFile (Join-Path $candidate "__init__.py")) -and
            (Test-NonEmptyFile (Join-Path $candidate "Inspyrenet_Rembg.py"))) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Get-PatchSetHash {
    # 内容指纹：两个补丁文件各自 SHA256 后拼接，用于判断是否需要重新应用补丁。
    param([string]$SourceDir)

    $initHash  = (Get-FileHash -LiteralPath (Join-Path $SourceDir "__init__.py")        -Algorithm SHA256).Hash
    $rembgHash = (Get-FileHash -LiteralPath (Join-Path $SourceDir "Inspyrenet_Rembg.py") -Algorithm SHA256).Hash
    return "$initHash-$rembgHash"
}

# -- main --
if (-not $Electron) {
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  ComfyUI configuration" -ForegroundColor White
    Write-Host "  Target: $DestDir" -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host ""
}

Emit "READY"
Emit "TOTAL" 2

$okCount = 0
$skipCount = 0
$failCount = 0

try {
    # -- pre-flight checks --
    if (-not (Test-NonEmptyFile $ComfyMain)) {
        throw "ComfyUI sentinel not found: $ComfyMain`nHas download-comfyui-engine.ps1 been run?"
    }
    if (-not (Test-NonEmptyFile $PythonExe -MinBytes (100 * 1024))) {
        throw "ComfyUI python_embeded not found: $PythonExe"
    }
    if (-not (Test-Path $InspyNodeDir)) {
        throw "Inspyrenet-Rembg custom node not found: $InspyNodeDir`nHas download-comfyui-engine.ps1 been run?"
    }

    # ================================================================
    # Step 1: Copy patched files from the on-disk source directory.
    # ================================================================
    $PatchSourceDir = Resolve-PatchSourceDir -BaseDir $DestDir
    if (-not $PatchSourceDir) {
        throw "Patched custom-node source not found.`nLooked for custom-node-patches\comfyui-inspyrenet-rembg next to this script, and at services\txt2img-api\custom_nodes\comfyui-inspyrenet-rembg."
    }
    $patchHash = Get-PatchSetHash -SourceDir $PatchSourceDir

    # 哨兵里存的是「上次已应用补丁的内容哈希」而非空文件：内容一变就重新应用。
    # 节点目录会被安装器跨版本保留，只有按内容判断才能让升级后的补丁更新生效。
    $appliedHash = if (Test-Path $PatchSentinel) {
        (Get-Content -LiteralPath $PatchSentinel -Raw -ErrorAction SilentlyContinue)
    } else { "" }
    if ($null -eq $appliedHash) { $appliedHash = "" }
    $appliedHash = $appliedHash.Trim()

    if ($appliedHash -eq $patchHash) {
        Finish-Skip "apply_patches" "already applied"
        $skipCount++
    } else {
        Emit "START" "apply_patches|2 files -> comfyui-inspyrenet-rembg"
        Write-Color "Apply patched files" Yellow
        if ($appliedHash) { Write-Step "patch content changed, re-applying" }
        Write-Step "source: $PatchSourceDir"

        # Copy-Item 逐字节复制，避免 Set-Content -Encoding UTF8 给 .py 加 BOM
        foreach ($fileName in @("__init__.py", "Inspyrenet_Rembg.py")) {
            Write-Step "writing $fileName"
            $destPath = Join-Path $InspyNodeDir $fileName
            Copy-Item -LiteralPath (Join-Path $PatchSourceDir $fileName) -Destination $destPath -Force
            if (-not (Test-NonEmptyFile $destPath)) { throw "failed to write $fileName" }
        }

        Set-Content -LiteralPath $PatchSentinel -Value $patchHash -Encoding Ascii -NoNewline
        Finish-Ok "apply_patches" "2 files"
        $okCount++
    }

    # ================================================================
    # Step 2: pip install required packages.
    # ================================================================
    if (Test-NonEmptyFile $PipSentinel) {
        Finish-Skip "pip_install" "already installed"
        $skipCount++
    } else {
        $pipPkgs = "transparent-background gguf protobuf resvg-py"
        Emit "START" "pip_install|$pipPkgs"
        Write-Color "pip install $pipPkgs" Yellow

        Write-Step "running: $PythonExe -m pip install $pipPkgs"
        if (-not $Electron) {
            # Manual mode: show live pip output.
            $pipProc = Start-Process -FilePath $PythonExe -ArgumentList @(
                "-m", "pip", "install", "transparent-background", "gguf", "protobuf", "resvg-py"
            ) -NoNewWindow -Wait -PassThru
            if ($pipProc.ExitCode -ne 0) {
                throw "pip install failed with exit code $($pipProc.ExitCode)"
            }
        } else {
            # Electron mode: capture output to log, show tail on error.
            $pipLog = Join-Path $ComfyPortable "pip-install.log"
            $pipProc = Start-Process -FilePath $PythonExe -ArgumentList @(
                "-m", "pip", "install", "transparent-background", "gguf", "protobuf", "resvg-py"
            ) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $pipLog -RedirectStandardError $pipLog
            if ($pipProc.ExitCode -ne 0) {
                $tail = if (Test-Path $pipLog) { Get-Content $pipLog -Tail 20 | Out-String } else { "" }
                throw "pip install failed with exit code $($pipProc.ExitCode)`n$tail"
            }
            Remove-Item -LiteralPath $pipLog -Force -ErrorAction SilentlyContinue
        }

        [void](New-Item -ItemType File -Force -Path $PipSentinel)
        Finish-Ok "pip_install" "packages installed"
        $okCount++
    }
} catch {
    $failCount++
    $msg = $_.Exception.Message -replace "\|", ":"
    Emit "ERROR" "configure|1|$msg"
    Write-Color "Error: $msg" Red
}

Emit "COMPLETE" "${okCount}|${skipCount}|${failCount}"

if (-not $Electron) {
    Write-Host ""
    if ($failCount -gt 0) {
        Write-Host "Finished with failures: $okCount ok, $skipCount skipped, $failCount failed" -ForegroundColor Yellow
    } else {
        Write-Host "All done: $okCount ok, $skipCount skipped" -ForegroundColor Green
    }
}

if ($failCount -gt 0) { exit 1 } else { exit 0 }
