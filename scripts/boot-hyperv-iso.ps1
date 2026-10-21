param(
    [string]$VmName = "webot-wechat",
    [int]$InjectSeconds = 20,
    [int]$IntervalMs = 200,
    [string]$LogPath = "C:\Users\qier\webot\data\hyperv-boot-iso.log"
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

Write-Log "Preparing VM: $VmName"
$vm = Get-VM -Name $VmName

if ($vm.State -ne "Off") {
    Write-Log "Stopping VM from state: $($vm.State)"
    Stop-VM -Name $VmName -TurnOff -Force
    Start-Sleep -Seconds 2
}

Write-Log "Starting VM"
Start-VM -Name $VmName

$keyboard = $null
for ($i = 0; $i -lt 40 -and -not $keyboard; $i++) {
    Start-Sleep -Milliseconds 250
    try {
        $keyboard = Get-VmKeyboard -Name $VmName
    } catch {
        $keyboard = $null
    }
}

if (-not $keyboard) {
    throw "Could not attach to VM keyboard after start."
}

Write-Log "Injecting boot keys for $InjectSeconds seconds"
$deadline = (Get-Date).AddSeconds($InjectSeconds)
while ((Get-Date) -lt $deadline) {
    Invoke-CimMethod -InputObject $keyboard -MethodName "TypeText" -Arguments @{ AsciiText = " " } | Out-Null
    Start-Sleep -Milliseconds $IntervalMs
}

Write-Log "VM state after key injection"
Get-VM -Name $VmName |
    Select-Object Name,State,Status,MemoryAssigned,MemoryStartup |
    Format-List |
    Out-File -LiteralPath $LogPath -Encoding UTF8 -Append

Write-Log "Done"
