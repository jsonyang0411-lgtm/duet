param(
    [string]$VmName = "webot-wechat",
    [string]$Text = "",
    [int[]]$KeyCode = @(),
    [int[]]$Scancode = @(),
    [int]$Repeat = 1,
    [int]$IntervalMs = 150
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated Administrator PowerShell."
    }
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

$keyboard = Get-VmKeyboard -Name $VmName
for ($i = 0; $i -lt $Repeat; $i++) {
    if ($Text.Length -gt 0) {
        Invoke-CimMethod -InputObject $keyboard -MethodName "TypeText" -Arguments @{ AsciiText = $Text } | Out-Null
    }

    foreach ($code in $KeyCode) {
        Invoke-CimMethod -InputObject $keyboard -MethodName "TypeKey" -Arguments @{ KeyCode = [uint32]$code } | Out-Null
        Start-Sleep -Milliseconds $IntervalMs
    }

    if ($Scancode.Count -gt 0) {
        $bytes = [byte[]]($Scancode | ForEach-Object { [byte]$_ })
        Invoke-CimMethod -InputObject $keyboard -MethodName "TypeScancodes" -Arguments @{ Scancodes = $bytes } | Out-Null
        Start-Sleep -Milliseconds $IntervalMs
    }

    if ($i -lt ($Repeat - 1)) {
        Start-Sleep -Milliseconds $IntervalMs
    }
}
