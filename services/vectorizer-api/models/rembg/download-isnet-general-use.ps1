<#
.SYNOPSIS
    Download the isnet-general-use.onnx model file and verify its MD5.
.DESCRIPTION
    Downloads the model from the official rembg GitHub release, computes the
    actual MD5 and compares it with the expected value; on mismatch it deletes the file and exits.
.NOTES
    Functionally identical to the .bat version in the same directory.
#>

$ErrorActionPreference = 'Stop'

$ModelName = 'isnet-general-use.onnx'
$ModelUrl  = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx'
$ModelMd5  = 'FC16EBD8B0C10D971D3513D564D01E29'
$ModelPath = Join-Path -Path $PSScriptRoot -ChildPath $ModelName

Write-Host "Downloading ${ModelName}..." -ForegroundColor Cyan

try {
    Invoke-WebRequest -Uri $ModelUrl -OutFile $ModelPath
} catch {
    Write-Host 'Download failed.' -ForegroundColor Red
    exit 1
}

Write-Host 'Verifying MD5 checksum...' -ForegroundColor Cyan
$actualMd5 = (Get-FileHash -Algorithm MD5 -LiteralPath $ModelPath).Hash

if ($actualMd5 -ne $ModelMd5) {
    Write-Host 'Checksum mismatch.' -ForegroundColor Red
    Write-Host "Expected: $ModelMd5"
    Write-Host "Actual:   $actualMd5"
    Remove-Item -LiteralPath $ModelPath -Force
    exit 1
}

Write-Host "Model ready: $ModelPath" -ForegroundColor Green
