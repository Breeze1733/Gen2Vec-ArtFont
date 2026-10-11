# download-comfyui-engine.ps1 - download and prepare ComfyUI portable runtime.
<#
.SYNOPSIS
Downloads 7za.exe, ComfyUI Windows portable, extracts it, removes the archive,
then downloads and installs the ComfyUI-GGUF and ComfyUI-Inspyrenet-Rembg custom nodes.

.PARAMETER DestDir
Target backend directory. Defaults to the script directory.

.PARAMETER Electron
Electron integration mode: prints ENGINEDL: structured progress lines.

.PARAMETER NoMirror
Use github.com directly instead of trying the mirror first.

.PARAMETER MaxRetries
Maximum retries per download. Default: 3.
#>

param(
    [string]$DestDir = $PSScriptRoot,
    [switch]$Electron,
    [switch]$NoMirror,
    [int]$MaxRetries = 3
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$PREFIX = if ($Electron) { "ENGINEDL:" } else { "" }

$SevenZipExe = Join-Path $DestDir "7za.exe"
$SevenZipZip = Join-Path $DestDir "7za920.zip"
$SevenZipUrl = "https://www.7-zip.org/a/7za920.zip"

$ComfyArchive = Join-Path $DestDir "ComfyUI_windows_portable_nvidia.7z"
$ComfyArchiveComplete = "$ComfyArchive.complete"
$ComfyArchiveMinBytes = 1800MB
$ComfyGithubUrl = "https://github.com/Comfy-Org/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z"
$ComfyMirrorUrls = @(
    "https://gh-proxy.com/$ComfyGithubUrl",
    "https://gh.llkk.cc/$ComfyGithubUrl",
    "https://ghproxy.net/$ComfyGithubUrl",
    "https://ghfast.top/$ComfyGithubUrl",
    "https://hub.gitmirror.com/$ComfyGithubUrl"
)
$EnvComfyUrls = @()
if ($env:COMFYUI_ENGINE_URLS) {
    $EnvComfyUrls = @($env:COMFYUI_ENGINE_URLS -split "[;,]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$ComfyUrls = if ($EnvComfyUrls.Count -gt 0) {
    $EnvComfyUrls
} elseif ($NoMirror) {
    @($ComfyGithubUrl)
} else {
    @($ComfyMirrorUrls + $ComfyGithubUrl)
}
$ComfyRoot = Join-Path $DestDir "ComfyUI_windows_portable_nvidia"
$ComfyPortable = Join-Path $ComfyRoot "ComfyUI_windows_portable"
$ComfyMain = Join-Path $ComfyPortable "ComfyUI\main.py"
$ComfyPython = Join-Path $ComfyPortable "python_embeded\python.exe"

$GgufZip = Join-Path $DestDir "ComfyUI-GGUF.zip"
$GgufGithubUrl = "https://github.com/city96/ComfyUI-GGUF/archive/refs/heads/main.zip"
$GgufMirrorUrls = @(
    "https://gh-proxy.com/$GgufGithubUrl",
    "https://gh.llkk.cc/$GgufGithubUrl",
    "https://ghproxy.net/$GgufGithubUrl",
    "https://ghfast.top/$GgufGithubUrl",
    "https://hub.gitmirror.com/$GgufGithubUrl"
)
$GgufUrls = if ($NoMirror) { @($GgufGithubUrl) } else { @($GgufMirrorUrls + $GgufGithubUrl) }
$CustomNodesDir = Join-Path $ComfyPortable "ComfyUI\custom_nodes"
$GgufNodeDir = Join-Path $CustomNodesDir "ComfyUI-GGUF"
$GgufSentinel = Join-Path $GgufNodeDir "nodes.py"

$InspyZip = Join-Path $DestDir "ComfyUI-Inspyrenet-Rembg.zip"
$InspyGithubUrl = "https://github.com/john-mnz/ComfyUI-Inspyrenet-Rembg/archive/refs/heads/main.zip"
$InspyMirrorUrls = @(
    "https://gh-proxy.com/$InspyGithubUrl",
    "https://gh.llkk.cc/$InspyGithubUrl",
    "https://ghproxy.net/$InspyGithubUrl",
    "https://ghfast.top/$InspyGithubUrl",
    "https://hub.gitmirror.com/$InspyGithubUrl"
)
$InspyUrls = if ($NoMirror) { @($InspyGithubUrl) } else { @($InspyMirrorUrls + $InspyGithubUrl) }
$InspyNodeDir = Join-Path $CustomNodesDir "ComfyUI-Inspyrenet-Rembg"

$ComfyConfigScript = Join-Path $DestDir "configure-comfyui.ps1"
$ComfyConfigSentinel = Join-Path $ComfyPortable ".configured"

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

function Format-Elapsed {
    param([timespan]$Elapsed)
    if ($Elapsed.TotalHours -ge 1) { return "{0:hh\:mm\:ss}" -f $Elapsed }
    return "{0:mm\:ss}" -f $Elapsed
}

function Invoke-SevenZipWithHeartbeat {
    param(
        [string]$SevenZipPath,
        [string]$WorkingDirectory,
        [string[]]$ArgumentList,
        [string]$Label,
        [string]$StdoutPath = "",
        [string]$StderrPath = ""
    )

    $start = Get-Date
    $args = @{
        FilePath = $SevenZipPath
        WorkingDirectory = $WorkingDirectory
        ArgumentList = $ArgumentList
        NoNewWindow = $true
        PassThru = $true
    }
    if ($StdoutPath) { $args.RedirectStandardOutput = $StdoutPath }
    if ($StderrPath) { $args.RedirectStandardError = $StderrPath }

    $p = Start-Process @args
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 3
        if (-not $Electron) {
            $elapsedText = Format-Elapsed -Elapsed ((Get-Date) - $start)
            Write-Host ("`r  {0} still running ({1})    " -f $Label, $elapsedText) -NoNewline -ForegroundColor DarkGray
        }
        try { $p.Refresh() } catch {}
    }

    if (-not $Electron) {
        $elapsedText = Format-Elapsed -Elapsed ((Get-Date) - $start)
        Write-Host ("`r  {0} finished ({1})        " -f $Label, $elapsedText) -ForegroundColor DarkGray
    }

    try { $p.Refresh() } catch {}
    $ec = if ($null -ne $p.ExitCode) { $p.ExitCode } else { 0 }
    # 7za: 0 = success, 1 = non-fatal warning, 2+ = fatal
    return $ec
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

function Test-SevenZipReady {
    Test-NonEmptyFile -Path $SevenZipExe -MinBytes (100 * 1024)
}

function Test-ComfyUIReady {
    (Test-NonEmptyFile -Path $ComfyMain) -and (Test-NonEmptyFile -Path $ComfyPython -MinBytes (100 * 1024))
}

function Test-GgufReady {
    Test-NonEmptyFile -Path $GgufSentinel
}

function Test-CustomNodeReady {
    param([string]$NodeDir)

    if (-not (Test-Path $NodeDir)) { return $false }
    $pyFiles = @(Get-ChildItem -LiteralPath $NodeDir -Recurse -Filter "*.py" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -gt 0 })
    return $pyFiles.Count -gt 0
}

function Test-InspyReady {
    Test-CustomNodeReady -NodeDir $InspyNodeDir
}

function Test-ComfyUIConfigured {
    Test-NonEmptyFile -Path $ComfyConfigSentinel
}

function Format-FileSize {
    param([long]$Bytes)
    if ($Bytes -lt 0) { return "?" }
    if ($Bytes -ge 1TB) { return "{0:N2} TB" -f ($Bytes / 1TB) }
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N2} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N2} KB" -f ($Bytes / 1KB) }
    return "$Bytes B"
}

function Get-RemoteFileSize {
    param([string]$Url)
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "HEAD"
        $req.Timeout = 30000
        $req.AllowAutoRedirect = $true
        $req.UserAgent = "Gen2Vec-ArtFont-EngineDownloader/1.0"
        $resp = $req.GetResponse()
        if ($resp.ContentLength -ge 0) {
            $size = [long]$resp.ContentLength
            $resp.Close()
            return $size
        }
        $resp.Close()
    } catch {}
    return -1
}

function Invoke-RangeDownload {
    param(
        [string]$Url,
        [string]$OutputPath
    )

    $existing = if (Test-Path $OutputPath) { [long](Get-Item $OutputPath).Length } else { 0 }
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Method = "GET"
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $request.AllowAutoRedirect = $true
    $request.UserAgent = "Gen2Vec-ArtFont-EngineDownloader/1.0"
    if ($existing -gt 0) {
        $request.AddRange($existing)
    }

    try {
        $response = $request.GetResponse()
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($resp -and [int]$resp.StatusCode -eq 416) {
            Remove-Item -LiteralPath $OutputPath -Force -ErrorAction SilentlyContinue
            $request = [System.Net.HttpWebRequest]::Create($Url)
            $request.Method = "GET"
            $request.Timeout = 30000
            $request.ReadWriteTimeout = 30000
            $request.AllowAutoRedirect = $true
            $request.UserAgent = "Gen2Vec-ArtFont-EngineDownloader/1.0"
            $response = $request.GetResponse()
            $existing = 0
        } else {
            throw
        }
    }

    try {
        $status = [int]$response.StatusCode
        if ($existing -gt 0 -and $status -eq 200) {
            $fileMode = [System.IO.FileMode]::Create
            $existing = 0
        } else {
            $fileMode = [System.IO.FileMode]::OpenOrCreate
        }

        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($OutputPath, $fileMode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        if ($existing -gt 0) {
            [void]$outputStream.Seek($existing, [System.IO.SeekOrigin]::Begin)
        } else {
            $outputStream.SetLength(0)
        }

        $buffer = New-Object byte[] (1024 * 1024)
        $lastProgressAt = Get-Date
        $lastBytes = $existing
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outputStream.Write($buffer, 0, $read)
            if (-not $Electron) {
                $now = Get-Date
                if (($now - $lastProgressAt).TotalSeconds -ge 2) {
                    $currentBytes = $outputStream.Position
                    $deltaBytes = $currentBytes - $lastBytes
                    $speedText = if ($deltaBytes -gt 0) { "$(Format-FileSize -Bytes ([long]($deltaBytes / [Math]::Max(1, ($now - $lastProgressAt).TotalSeconds))))/s" } else { "0 B/s" }
                    Write-Host ("`r  Downloaded {0} ({1})    " -f (Format-FileSize -Bytes $currentBytes), $speedText) -NoNewline -ForegroundColor DarkGray
                    $lastProgressAt = $now
                    $lastBytes = $currentBytes
                }
            }
        }
        if (-not $Electron) {
            Write-Host ("`r  Downloaded {0} complete        " -f (Format-FileSize -Bytes $outputStream.Position)) -ForegroundColor DarkGray
        }
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Close() }
    }
}

function Invoke-DownloadWithRetry {
    param(
        [string]$Name,
        [string]$Url,
        [string]$OutputPath,
        [string]$SizeText
    )

    for ($i = 0; $i -lt $MaxRetries; $i++) {
        try {
            if ($i -gt 0) {
                Write-Step "retry $($i + 1)/$MaxRetries for $Name"
            }
            Invoke-RangeDownload -Url $Url -OutputPath $OutputPath
            return
        } catch {
            $lastError = $_.Exception.Message
            if ($i -ge $MaxRetries - 1) {
                throw "${Name}: ${lastError}"
            }
            Start-Sleep -Seconds 3
        }
    }
}

function Invoke-DownloadWithFallback {
    param(
        [string]$Name,
        [string[]]$Urls,
        [string]$OutputPath,
        [string]$SizeText
    )

    $errors = @()
    for ($u = 0; $u -lt $Urls.Count; $u++) {
        $url = $Urls[$u]
        $label = $url
        try { $label = ([System.Uri]$url).Host } catch {}
        Write-Color "  source: $label" DarkGray
        try {
            Invoke-DownloadWithRetry -Name $Name -Url $url -OutputPath $OutputPath -SizeText $SizeText
            return
        } catch {
            $errors += "${label}: $($_.Exception.Message)"
        }
    }

    throw "${Name}: all sources failed ($($errors -join '; '))"
}

function Get-RemoteFileSizeFromFallback {
    param([string[]]$Urls)

    foreach ($url in $Urls) {
        $label = $url
        try { $label = ([System.Uri]$url).Host } catch {}
        Write-Step "check remote size: $label"
        $size = Get-RemoteFileSize -Url $url
        if ($size -gt 0) { return $size }
    }
    return -1
}

function Test-SevenZipArchive {
    param(
        [string]$ArchivePath,
        [string]$SevenZipPath
    )

    if (-not (Test-NonEmptyFile -Path $ArchivePath) -or -not (Test-NonEmptyFile -Path $SevenZipPath -MinBytes (100 * 1024))) {
        return $false
    }

    try {
        $archiveDir = Split-Path $ArchivePath -Parent
        $archiveName = Split-Path $ArchivePath -Leaf
        Write-Step "test archive integrity: $archiveName"
        $exitCode = Invoke-SevenZipWithHeartbeat -SevenZipPath $SevenZipPath -WorkingDirectory $archiveDir -ArgumentList @(
            't',
            $archiveName
        ) -Label "archive integrity test" -StdoutPath "$env:TEMP\gen2vec-7za-test.out" -StderrPath "$env:TEMP\gen2vec-7za-test.err"
        return $exitCode -eq 0
    } catch {
        return $false
    }
}

function Test-ComfyArchiveSizeReady {
    if (-not (Test-NonEmptyFile -Path $ComfyArchive -MinBytes $ComfyArchiveMinBytes)) {
        return $false
    }

    $localBytes = [long](Get-Item -LiteralPath $ComfyArchive).Length
    $localText = Format-FileSize -Bytes $localBytes
    $minText = Format-FileSize -Bytes $ComfyArchiveMinBytes
    Write-Step "archive size accepted: $localText (minimum $minText)"
    return $true
}

function Normalize-ComfyUIExtraction {
    # After 7z extraction the directory is always DestDir/ComfyUI_windows_portable
    $extractedDir = Join-Path $DestDir "ComfyUI_windows_portable"

    # Move it: wrap it in a ComfyUI_windows_portable_nvidia parent directory
    New-Item -ItemType Directory -Force -Path $ComfyRoot | Out-Null
    Move-Item -LiteralPath $extractedDir -Destination $ComfyPortable -Force

    # Check main.py after the move
    if (-not (Test-ComfyUIReady)) {
        throw "ComfyUI/main.py not found after moving to $ComfyPortable"
    }
}

function Ensure-ZipAssembly {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
}

function Expand-ZipToDirectory {
    param([string]$ZipPath, [string]$Destination)
    Ensure-ZipAssembly
    if (Test-Path $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $Destination)
}

function Finish-Ok {
    param([string]$Name, [string]$Size = "")
    if ($Size) { Emit "DONE" "${Name}|${Size}" }
    else { Emit "DONE" $Name }
    Write-Color "OK $Name" Green
}

function Finish-Skip {
    param([string]$Name, [string]$Size = "")
    if ($Size) { Emit "SKIP" "${Name}|${Size}" }
    else { Emit "SKIP" $Name }
    Write-Color "Skip $Name" DarkGray
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

if (-not $Electron) {
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  ComfyUI engine downloader" -ForegroundColor White
    Write-Host "  Target: $DestDir" -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host ""
}

Emit "READY"
Emit "TOTAL" 6

$okCount = 0
$skipCount = 0
$failCount = 0

try {
    # 1. Download 7za.exe.
    $sevenZipReady = Test-SevenZipReady
    if ($sevenZipReady) {
        $sz = Format-FileSize -Bytes (Get-Item $SevenZipExe).Length
        Finish-Skip "7za.exe" $sz
        $skipCount++
    } else {
        $sizeText = "1.10 MB"
        Emit "START" "7za.exe|$sizeText"
        Write-Color "Download 7za.exe ($sizeText)" Yellow
        Invoke-DownloadWithRetry -Name "7za.exe" -Url $SevenZipUrl -OutputPath $SevenZipZip -SizeText $sizeText

        $tmp7z = Join-Path $DestDir "_7za_extract"
        Expand-ZipToDirectory -ZipPath $SevenZipZip -Destination $tmp7z
        $extracted = Get-ChildItem -LiteralPath $tmp7z -Recurse -Filter "7za.exe" -File | Select-Object -First 1
        if (-not $extracted) { throw "7za.exe not found inside downloaded archive" }
        Copy-Item -LiteralPath $extracted.FullName -Destination $SevenZipExe -Force
        Remove-Item -LiteralPath $tmp7z -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $SevenZipZip -Force -ErrorAction SilentlyContinue
        $sz = Format-FileSize -Bytes (Get-Item $SevenZipExe).Length
        Finish-Ok "7za.exe" $sz
        $okCount++
    }

    # 2. Download ComfyUI portable archive.
    if (Test-ComfyUIReady) {
        Finish-Skip "ComfyUI_portable" "installed"
        $skipCount++
    } else {
        $needsDownload = $true
        $sizeText = "2 GB"
        $existingSize = if (Test-Path $ComfyArchive) { [long](Get-Item $ComfyArchive).Length } else { 0 }

        if ($existingSize -gt 0) {
            if (Test-Path $ComfyArchiveComplete) {
                # .complete marker present -> download is complete
                $sz = Format-FileSize -Bytes $existingSize
                Finish-Skip "ComfyUI_portable" $sz
                $skipCount++
                $needsDownload = $false
            } else {
                # partial file, no .complete marker -> compare against the remote size
                $remoteBytes = Get-RemoteFileSizeFromFallback -Urls $ComfyUrls
                $localStr = Format-FileSize -Bytes $existingSize
                if ($remoteBytes -gt 0 -and $existingSize -eq $remoteBytes) {
                    # size matches remote -> write the marker and skip the download
                    [void](New-Item -ItemType File -Force -Path $ComfyArchiveComplete)
                    Finish-Skip "ComfyUI_portable" $localStr
                    $skipCount++
                    $needsDownload = $false
                } else {
                    # size differs -> resume
                    $remoteStr = if ($remoteBytes -gt 0) { Format-FileSize -Bytes $remoteBytes } else { "?" }
                    Emit "CHECK" "ComfyUI_portable|${localStr}|${remoteStr}"
                    Write-Color "Check ComfyUI portable - local ${localStr} / remote ${remoteStr}" Cyan
                    Emit "RESUME" "ComfyUI_portable|${localStr}"
                    Write-Color "Resume ComfyUI portable (existing ${localStr})" Yellow
                }
            }
        }

        if ($needsDownload) {
            Emit "START" "ComfyUI_portable|$sizeText"
            Write-Color "Download ComfyUI portable ($sizeText)" Yellow

            Invoke-DownloadWithFallback -Name "ComfyUI_portable" -Urls $ComfyUrls -OutputPath $ComfyArchive -SizeText $sizeText
            Emit "START" "ComfyUI_portable|checking size"
            if (-not (Test-ComfyArchiveSizeReady)) {
                $actualSize = Format-FileSize -Bytes ([long](Get-Item -LiteralPath $ComfyArchive).Length)
                $minSize = Format-FileSize -Bytes $ComfyArchiveMinBytes
                throw "ComfyUI archive is too small after download: $actualSize, expected at least $minSize"
            }
            [void](New-Item -ItemType File -Force -Path $ComfyArchiveComplete)
            $sz = Format-FileSize -Bytes (Get-Item $ComfyArchive).Length
            Finish-Ok "ComfyUI_portable" $sz
            $okCount++
        }
    }

    # 3. Extract ComfyUI archive and delete it after success.
    $extractedDir = Join-Path $DestDir "ComfyUI_windows_portable"
    if (Test-Path $ComfyPortable) {
        # target path exists and main.py is ready -> skip
        if (Test-ComfyUIReady) {
            Finish-Skip "Extract_ComfyUI" "installed"
            $skipCount++
        } else {
            # dir exists but main.py is gone: a previous move broke it -> delete and rebuild
            Write-Step "corrupted $ComfyPortable, re-extract"
            Remove-Item -LiteralPath $ComfyPortable -Recurse -Force
            # fall through to the else branch below
        }
    }
    if (-not (Test-Path $ComfyPortable)) {
        Emit "START" "Extract_ComfyUI|local"
        $extractedDir = Join-Path $DestDir "ComfyUI_windows_portable"
        if (Test-Path $extractedDir) {
            Write-Color "Use existing extracted ComfyUI portable" Yellow
            Write-Step "found extracted folder: $extractedDir"
        } else {
            if (-not (Test-Path $ComfyArchive)) { throw "ComfyUI archive missing: $ComfyArchive" }
            Write-Color "Extract ComfyUI portable" Yellow
            Write-Step "this can take several minutes for the 2 GB archive"
            if (Test-Path $ComfyRoot) {
                Write-Step "remove previous normalized directory"
                Remove-Item -LiteralPath $ComfyRoot -Recurse -Force
            }
            $exitCode = Invoke-SevenZipWithHeartbeat -SevenZipPath $SevenZipExe -WorkingDirectory $DestDir -ArgumentList @(
                'x',
                '-y',
                '-o.',
                (Split-Path $ComfyArchive -Leaf)
            ) -Label "archive extraction"
            if ($exitCode -eq 2) { throw "7za extract failed: corrupt archive or disk full" }
            if ($exitCode -gt 2) { throw "7za extract failed with exit code $exitCode" }
        }
        Write-Step "move extracted folder to $ComfyPortable"
        Normalize-ComfyUIExtraction
        Remove-Item -LiteralPath $ComfyArchive -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ComfyArchiveComplete -Force -ErrorAction SilentlyContinue
        Finish-Ok "Extract_ComfyUI" "ready"
        $okCount++
    }

    # 4. Download and install ComfyUI-GGUF custom node.
    if (Test-GgufReady) {
        Finish-Skip "ComfyUI-GGUF" "installed"
        $skipCount++
    } else {
        New-Item -ItemType Directory -Force -Path $CustomNodesDir | Out-Null
        $needsDownload = $true
        if (Test-Path $GgufZip) {
            $needsDownload = $false
        }

        if ($needsDownload) {
            $sizeText = "200 KB"
            Emit "START" "ComfyUI-GGUF|$sizeText"
            Write-Color "Download ComfyUI-GGUF ($sizeText)" Yellow
            Invoke-DownloadWithFallback -Name "ComfyUI-GGUF" -Urls $GgufUrls -OutputPath $GgufZip -SizeText $sizeText
        } else {
            $sizeText = Format-FileSize -Bytes (Get-Item $GgufZip).Length
            Emit "START" "ComfyUI-GGUF|$sizeText"
            Write-Color "Use bundled ComfyUI-GGUF.zip ($sizeText)" Yellow
        }

        $tmpGguf = Join-Path $DestDir "_ComfyUI-GGUF_extract"
        Expand-ZipToDirectory -ZipPath $GgufZip -Destination $tmpGguf
        $sourceDir = Get-ChildItem -LiteralPath $tmpGguf -Directory | Select-Object -First 1
        if (-not $sourceDir) { throw "ComfyUI-GGUF source directory not found after extraction" }
        if (Test-Path $GgufNodeDir) {
            Remove-Item -LiteralPath $GgufNodeDir -Recurse -Force
        }
        Move-Item -LiteralPath $sourceDir.FullName -Destination $GgufNodeDir -Force
        Remove-Item -LiteralPath $tmpGguf -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-GgufReady)) { throw "ComfyUI-GGUF files not found after install" }
        $sz = Format-FileSize -Bytes (Get-Item $GgufZip).Length
        Remove-Item -LiteralPath $GgufZip -Force -ErrorAction SilentlyContinue
        Finish-Ok "ComfyUI-GGUF" $sz
        $okCount++
    }

    # 5. Download and install ComfyUI-Inspyrenet-Rembg custom node.
    if (Test-InspyReady) {
        Finish-Skip "ComfyUI-Inspyrenet-Rembg" "installed"
        $skipCount++
    } else {
        New-Item -ItemType Directory -Force -Path $CustomNodesDir | Out-Null
        $needsDownload = $true
        if (Test-Path $InspyZip) {
            $needsDownload = $false
        }

        if ($needsDownload) {
            $sizeText = "1 MB"
            Emit "START" "ComfyUI-Inspyrenet-Rembg|$sizeText"
            Write-Color "Download ComfyUI-Inspyrenet-Rembg ($sizeText)" Yellow
            Invoke-DownloadWithFallback -Name "ComfyUI-Inspyrenet-Rembg" -Urls $InspyUrls -OutputPath $InspyZip -SizeText $sizeText
        } else {
            $sizeText = Format-FileSize -Bytes (Get-Item $InspyZip).Length
            Emit "START" "ComfyUI-Inspyrenet-Rembg|$sizeText"
            Write-Color "Use bundled ComfyUI-Inspyrenet-Rembg.zip ($sizeText)" Yellow
        }

        $tmpInspy = Join-Path $DestDir "_ComfyUI-Inspyrenet-Rembg_extract"
        Expand-ZipToDirectory -ZipPath $InspyZip -Destination $tmpInspy
        $sourceDir = Get-ChildItem -LiteralPath $tmpInspy -Directory | Select-Object -First 1
        if (-not $sourceDir) { throw "ComfyUI-Inspyrenet-Rembg source directory not found after extraction" }
        if (Test-Path $InspyNodeDir) {
            Remove-Item -LiteralPath $InspyNodeDir -Recurse -Force
        }
        Move-Item -LiteralPath $sourceDir.FullName -Destination $InspyNodeDir -Force
        Remove-Item -LiteralPath $tmpInspy -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-InspyReady)) { throw "ComfyUI-Inspyrenet-Rembg files not found after install" }
        $sz = Format-FileSize -Bytes (Get-Item $InspyZip).Length
        Remove-Item -LiteralPath $InspyZip -Force -ErrorAction SilentlyContinue
        Finish-Ok "ComfyUI-Inspyrenet-Rembg" $sz
        $okCount++
    }

    # 6. Configure ComfyUI (run configure script).
    if (Test-ComfyUIConfigured) {
        Finish-Skip "ComfyUI_config" "configured"
        $skipCount++
    } else {
        Emit "START" "ComfyUI_config|script"
        Write-Color "Configure ComfyUI" Yellow
        if (-not (Test-Path $ComfyConfigScript)) {
            Write-Step "configure script not found, skip: $ComfyConfigScript"
            Finish-Skip "ComfyUI_config" "no-script"
            $skipCount++
        } else {
            Write-Step "running: $ComfyConfigScript"
            $configResult = & powershell -NoProfile -ExecutionPolicy Bypass -File $ComfyConfigScript
            if ($LASTEXITCODE -ne 0) {
                throw "ComfyUI configure script failed with exit code $LASTEXITCODE"
            }
            [void](New-Item -ItemType File -Force -Path $ComfyConfigSentinel)
            Finish-Ok "ComfyUI_config" "done"
            $okCount++
        }
    }
} catch {
    $failCount++
    $msg = $_.Exception.Message -replace "\|", ":"
    $name = if ($_.InvocationInfo -and $_.InvocationInfo.MyCommand) { $_.InvocationInfo.MyCommand.Name } else { "script" }
    Emit "ERROR" "${name}|1|$msg"
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
