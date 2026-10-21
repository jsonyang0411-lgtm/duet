param(
    [string]$VmName = "webot-wechat",
    [Parameter(Mandatory = $true)]
    [string]$IsoPath,
    [string]$VmRoot = "E:\HyperV",
    [string]$ProjectPath = "C:\Users\qier\webot",
    [int]$MemoryGB = 4,
    [int]$StartupMemoryGB = 3,
    [int]$CpuCount = 4,
    [int]$VhdSizeGB = 70,
    [int]$MinFreeGB = 50,
    [switch]$CreateHostShare
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated Administrator PowerShell."
    }
}

function Get-PathDriveFreeGB([string]$Path) {
    $qualified = Split-Path -Qualifier $Path
    if (-not $qualified) {
        $qualified = (Get-Location).Drive.Root
    }
    $driveName = $qualified.TrimEnd(":\")
    $drive = Get-PSDrive -Name $driveName -PSProvider FileSystem
    return [math]::Round($drive.Free / 1GB, 1)
}

Assert-Admin

if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) {
    throw "Hyper-V PowerShell module is not available on this machine."
}

if (-not (Test-Path -LiteralPath $IsoPath)) {
    throw "Windows ISO not found: $IsoPath"
}

if (-not (Test-Path -LiteralPath $ProjectPath)) {
    throw "Project path not found: $ProjectPath"
}

$freeGB = Get-PathDriveFreeGB $VmRoot
if ($freeGB -lt $MinFreeGB) {
    throw "Not enough free space on VM drive. Need at least ${MinFreeGB}GB, found ${freeGB}GB for $VmRoot."
}

if (Get-VM -Name $VmName -ErrorAction SilentlyContinue) {
    throw "VM already exists: $VmName"
}

$switch = Get-VMSwitch -Name "Default Switch" -ErrorAction SilentlyContinue
if (-not $switch) {
    $switch = Get-VMSwitch | Select-Object -First 1
}
if (-not $switch) {
    throw "No Hyper-V virtual switch found. Create a switch first or enable Default Switch."
}

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null
$vhdPath = Join-Path $VmRoot "$VmName\$VmName.vhdx"
$memoryBytes = [int64]$MemoryGB * 1GB
$startupMemoryBytes = [int64]$StartupMemoryGB * 1GB
$vhdBytes = [int64]$VhdSizeGB * 1GB

New-VM `
    -Name $VmName `
    -Generation 2 `
    -MemoryStartupBytes $startupMemoryBytes `
    -NewVHDPath $vhdPath `
    -NewVHDSizeBytes $vhdBytes `
    -Path $VmRoot `
    -SwitchName $switch.Name | Out-Null

Set-VMProcessor -VMName $VmName -Count $CpuCount
Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $true -MinimumBytes 2GB -StartupBytes $startupMemoryBytes -MaximumBytes $memoryBytes

Set-VMKeyProtector -VMName $VmName -NewLocalKeyProtector
Enable-VMTPM -VMName $VmName
Set-VMFirmware -VMName $VmName -EnableSecureBoot On -SecureBootTemplate "MicrosoftWindows"

Add-VMDvdDrive -VMName $VmName -Path $IsoPath
$dvd = Get-VMDvdDrive -VMName $VmName
$disk = Get-VMHardDiskDrive -VMName $VmName
Set-VMFirmware -VMName $VmName -FirstBootDevice $dvd

Enable-VMIntegrationService -VMName $VmName -Name "Guest Service Interface" -ErrorAction SilentlyContinue
Set-VM -Name $VmName -CheckpointType Disabled

if ($CreateHostShare) {
    $share = Get-SmbShare -Name "webot" -ErrorAction SilentlyContinue
    if (-not $share) {
        New-SmbShare -Name "webot" -Path $ProjectPath -ChangeAccess $env:USERNAME | Out-Null
    }
}

Start-VM -Name $VmName

$vmconnect = Join-Path $env:WINDIR "System32\vmconnect.exe"
if (Test-Path $vmconnect) {
    Start-Process $vmconnect -ArgumentList @("localhost", $VmName)
}

Write-Host ""
Write-Host "VM created and started: $VmName"
Write-Host "Install Windows from the mounted ISO, then install WeChat and Python inside the VM."
Write-Host "After Windows setup, map the shared project folder or open it via the host share:"
Write-Host "  \\$env:COMPUTERNAME\webot"
Write-Host "Then run inside the VM:"
Write-Host "  .\scripts\start-vm-bot.ps1"
