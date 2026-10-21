param(
    [string]$Destination = "D:\ISO\Win11_25H2_Chinese_Simplified_x64.iso",
    [string]$DownloadUrl = ""
)

$ErrorActionPreference = "Stop"

if (-not $DownloadUrl) {
    throw "DownloadUrl is required. Generate a Microsoft ISO URL from the official download page first."
}

$destDir = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $destDir | Out-Null

Write-Host "Downloading ISO:"
Write-Host "  $Destination"

Start-BitsTransfer -Source $DownloadUrl -Destination $Destination -DisplayName "Windows 11 ISO"

if (-not (Test-Path -LiteralPath $Destination)) {
    throw "Download did not create expected file: $Destination"
}

$file = Get-Item -LiteralPath $Destination
Write-Host "Downloaded $([math]::Round($file.Length / 1GB, 2)) GB"

$hash = Get-FileHash -LiteralPath $Destination -Algorithm SHA256
Write-Host "SHA256: $($hash.Hash)"

return $Destination
