param(
    [string]$VmName = "webot-wechat",
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [string]$LogPath = "C:\Users\qier\webot\data\hyperv-oobe-command.log"
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
Invoke-CimMethod `
    -InputObject $keyboard `
    -MethodName "TypeScancodes" `
    -Arguments @{ Scancodes = [byte[]](42, 68, 196, 170) } |
    Out-Null

Start-Sleep -Seconds 3

Write-Log "Typing command: $Command"
Invoke-CimMethod `
    -InputObject $keyboard `
    -MethodName "TypeText" `
    -Arguments @{ AsciiText = ($Command + "`r") } |
    Out-Null

Write-Log "Done"
