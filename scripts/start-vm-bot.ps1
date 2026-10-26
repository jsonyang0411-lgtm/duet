param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = "C:\webot-runtime",
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

function Resolve-NativeFileSystemPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = Resolve-Path -LiteralPath $Path
    if ($resolved.Provider.Name -eq "FileSystem") {
        return $resolved.ProviderPath
    }

    return $resolved.Path
}

function Get-DefaultWeChatDataDir {
    $candidates = @(
        (Join-Path $env:USERPROFILE "Documents\xwechat_files"),
        (Join-Path $env:USERPROFILE "Documents\WeChat Files")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    return (Join-Path $env:USERPROFILE "Documents\WeChat Files")
}

function Ensure-LocalEnvEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [AllowEmptyString()][string]$Value
    )

    $content = ""
    if (Test-Path -LiteralPath $Path) {
        $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if ($content -match "(?m)^\s*$([regex]::Escape($Key))\s*=") {
            return
        }
    }

    $line = "$Key=$Value"
    if ($content -and -not $content.EndsWith("`n")) {
        Add-Content -LiteralPath $Path -Encoding UTF8 -Value ""
    }
    Add-Content -LiteralPath $Path -Encoding UTF8 -Value $line
}

function Get-EnvFileEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=(.*)$") {
            return $matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Set-EnvFileEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [AllowEmptyString()][string]$Value
    )

    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    }

    $found = $false
    $lines = @($lines | ForEach-Object {
        if ($_ -match "^\s*$([regex]::Escape($Key))\s*=") {
            $found = $true
            "$Key=$Value"
        } else {
            $_
        }
    })
    if (-not $found) {
        $lines += "$Key=$Value"
    }
    Set-Content -LiteralPath $Path -Encoding UTF8 -Value $lines
}

function Test-WcdbKey {
    param([string]$Key)
    return ($Key -match "^[0-9a-fA-F]{64}$")
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Resolve-NativeFileSystemPath (Join-Path $PSScriptRoot "..")
} else {
    $ProjectRoot = Resolve-NativeFileSystemPath $ProjectRoot
}

$venvRoot = Join-Path $RuntimeRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$localEnvDir = Join-Path $env:APPDATA "webot"
$localEnvFile = Join-Path $localEnvDir ".env.local"

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path $localEnvDir | Out-Null

if (-not (Test-Path $venvPython)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & py -3 -m venv $venvRoot
    } else {
        & python -m venv $venvRoot
    }
}

if (-not $NoInstall) {
    $requirementsFile = Join-Path $ProjectRoot "requirements.txt"
    & $venvPython -m pip install -r $requirementsFile
}

if (-not (Test-Path $localEnvFile)) {
    @"
# Machine-local overrides for webot.
# This file lives inside the VM, outside the shared project folder.
#
# Put VM-only values here, for example:
# WECHAT_DATA_DIR=C:\Users\<vm-user>\Documents\WeChat Files
# WCDB_KEY=...
#
# Shared settings such as CLAUDE_RELAY, PRIVATE_CHATS and the relay token can
# stay in the shared project .env.
"@ | Set-Content -Encoding UTF8 $localEnvFile
    Write-Warning "Created local env file: $localEnvFile"
    Write-Warning "Fill WECHAT_DATA_DIR and WCDB_KEY in that file before first run."
}

Ensure-LocalEnvEntry -Path $localEnvFile -Key "WECHAT_DATA_DIR" -Value (Get-DefaultWeChatDataDir)

$sharedEnvFile = Join-Path $ProjectRoot ".env"
$localDataDir = Get-EnvFileEntry -Path $localEnvFile -Key "WECHAT_DATA_DIR"
if ([string]::IsNullOrWhiteSpace($localDataDir)) {
    $localDataDir = Get-DefaultWeChatDataDir
    Set-EnvFileEntry -Path $localEnvFile -Key "WECHAT_DATA_DIR" -Value $localDataDir
}
$env:WECHAT_DATA_DIR = $localDataDir

$localKey = Get-EnvFileEntry -Path $localEnvFile -Key "WCDB_KEY"
$sharedKey = Get-EnvFileEntry -Path $sharedEnvFile -Key "WCDB_KEY"
if (Test-WcdbKey $localKey) {
    $env:WCDB_KEY = $localKey
    Write-Host "Using WCDB_KEY from local env (len=64)."
} elseif (Test-WcdbKey $sharedKey) {
    $env:WCDB_KEY = $sharedKey
    Write-Host "Using WCDB_KEY from shared env (len=64)."
} else {
    Write-Warning "WCDB_KEY is not available yet; use the dashboard key extraction flow."
}
Write-Host "Using WECHAT_DATA_DIR=$env:WECHAT_DATA_DIR"

$env:WEBOT_LOCAL_ENV_FILE = $localEnvFile
Set-Location $ProjectRoot
& $venvPython desktop.py
