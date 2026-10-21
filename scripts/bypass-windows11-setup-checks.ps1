param(
    [string]$VmName = "webot-wechat",
    [string]$LogPath = "C:\Users\qier\webot\data\windows11-setup-bypass.log"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated Administrator PowerShell."
    }
}

function Write-Log([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    "$stamp $Message" | Out-File -LiteralPath $LogPath -Encoding UTF8 -Append
}

function Get-VmKeyboard([string]$Name) {
    $vmSystem = Get-CimInstance `
        -Namespace "root\virtualization\v2" `
        -ClassName "Msvm_ComputerSystem" `
        -Filter "ElementName='$Name'"

    if (-not $vmSystem) {
        throw "Msvm_ComputerSystem not found for VM: $Name"
    }

    $keyboard = Get-CimAssociatedInstance -InputObject $vmSystem -ResultClassName "Msvm_Keyboard" |
        Select-Object -First 1

    if (-not $keyboard) {
        throw "Msvm_Keyboard not found for VM: $Name"
    }

    return $keyboard
}

function Send-Text([ciminstance]$Keyboard, [string]$Text) {
    Invoke-CimMethod `
        -InputObject $Keyboard `
        -MethodName "TypeText" `
        -Arguments @{ AsciiText = $Text } | Out-Null
}

function Send-Scancodes([ciminstance]$Keyboard, [byte[]]$Codes) {
    Invoke-CimMethod `
        -InputObject $Keyboard `
        -MethodName "TypeScancodes" `
        -Arguments @{ Scancodes = $Codes } | Out-Null
}

Assert-Admin

$logDir = Split-Path -Parent $LogPath
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
if (Test-Path -LiteralPath $LogPath) {
    Remove-Item -LiteralPath $LogPath -Force
}

$keyboard = Get-VmKeyboard -Name $VmName
Write-Log "Opening command prompt with Shift+F10"
Send-Scancodes -Keyboard $keyboard -Codes ([byte[]](42, 68, 196, 170))
Start-Sleep -Seconds 3

$commands = @(
    "reg add HKLM\SYSTEM\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f",
    "reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f",
    "reg add HKLM\SYSTEM\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f",
    "reg add HKLM\SYSTEM\Setup\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f",
    "exit"
)

foreach ($command in $commands) {
    Write-Log "Typing: $command"
    Send-Text -Keyboard $keyboard -Text ($command + "`r")
    Start-Sleep -Milliseconds 700
}

Write-Log "Done"
