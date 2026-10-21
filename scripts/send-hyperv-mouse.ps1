param(
    [string]$VmName = "webot-wechat",
    [int]$X = -1,
    [int]$Y = -1,
    [string]$ClickSpec = "",
    [int]$Button = 1,
    [int]$Repeat = 1,
    [int]$IntervalMs = 300,
    [string]$LogPath = "C:\Users\qier\webot\data\hyperv-mouse.log"
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

function Get-VmPart([string]$Name, [string]$ClassName) {
    $vmSystem = Get-CimInstance `
        -Namespace "root\virtualization\v2" `
        -ClassName "Msvm_ComputerSystem" `
        -Filter "ElementName='$Name'"

    if (-not $vmSystem) {
        throw "Msvm_ComputerSystem not found for VM: $Name"
    }

    $part = Get-CimAssociatedInstance -InputObject $vmSystem -ResultClassName $ClassName |
        Select-Object -First 1

    if (-not $part) {
        throw "$ClassName not found for VM: $Name"
    }

    return $part
}

Assert-Admin

$logDir = Split-Path -Parent $LogPath
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
if (Test-Path -LiteralPath $LogPath) {
    Remove-Item -LiteralPath $LogPath -Force
}

$mouse = Get-VmPart -Name $VmName -ClassName "Msvm_SyntheticMouse"
Write-Log "Mouse: $($mouse.ElementName)"

$points = @()
if ($ClickSpec.Trim().Length -gt 0) {
    foreach ($pair in $ClickSpec.Split(";")) {
        $parts = $pair.Split(",")
        if ($parts.Count -ne 2) {
            throw "Invalid click pair: $pair"
        }
        $points += [pscustomobject]@{
            X = [int]$parts[0]
            Y = [int]$parts[1]
        }
    }
} elseif ($X -ge 0 -and $Y -ge 0) {
    $points += [pscustomobject]@{ X = $X; Y = $Y }
} else {
    throw "Provide either -X/-Y or -ClickSpec."
}

for ($i = 0; $i -lt $Repeat; $i++) {
    foreach ($point in $points) {
        $move = Invoke-CimMethod `
            -InputObject $mouse `
            -MethodName "SetAbsolutePosition" `
            -Arguments @{ HorizontalPosition = [int]$point.X; VerticalPosition = [int]$point.Y }

        Write-Log "SetAbsolutePosition($($point.X),$($point.Y)) returned $($move.ReturnValue)"
        Start-Sleep -Milliseconds 100

        $click = Invoke-CimMethod `
            -InputObject $mouse `
            -MethodName "ClickButton" `
            -Arguments @{ ButtonIndex = [uint32]$Button }

        Write-Log "ClickButton($Button) returned $($click.ReturnValue)"
        Start-Sleep -Milliseconds $IntervalMs
    }
}

Write-Log "Done"
