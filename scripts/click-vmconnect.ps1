param(
    [string]$VmName = "webot-wechat",
    [string]$ClickSpec = "",
    [int]$InitialDelayMs = 800,
    [int]$DelayAfterClickMs = 400,
    [int]$FinalDelayMs = 1500,
    [switch]$RestartConnection,
    [string]$LogPath = "C:\Users\qier\webot\data\vmconnect-click.log"
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

Assert-Admin

$logDir = Split-Path -Parent $LogPath
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
if (Test-Path -LiteralPath $LogPath) {
    Remove-Item -LiteralPath $LogPath -Force
}

Add-Type @"
using System;
using System.Runtime.InteropServices;

public class VmConnectInput {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, UInt32 uFlags);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(UInt32 dwFlags, UInt32 dx, UInt32 dy, UInt32 dwData, UIntPtr dwExtraInfo);

    public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
    public const int SW_RESTORE = 9;
    public const UInt32 SWP_NOMOVE = 0x0002;
    public const UInt32 SWP_NOSIZE = 0x0001;
    public const UInt32 SWP_SHOWWINDOW = 0x0040;
    public const UInt32 MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const UInt32 MOUSEEVENTF_LEFTUP = 0x0004;
}

public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}
"@

function Find-VmConnectWindow([string]$Name) {
    $found = New-Object System.Collections.Generic.List[System.IntPtr]
    $callback = [VmConnectInput+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        $length = [VmConnectInput]::GetWindowTextLength($hWnd)
        if ($length -le 0) {
            return $true
        }

        $builder = New-Object System.Text.StringBuilder ($length + 1)
        [void][VmConnectInput]::GetWindowText($hWnd, $builder, $builder.Capacity)
        $title = $builder.ToString()
        if ($title.Contains($Name) -and $title.Contains("虚拟机连接")) {
            [void]$found.Add($hWnd)
        }
        return $true
    }

    [void][VmConnectInput]::EnumWindows($callback, [IntPtr]::Zero)
    if ($found.Count -eq 0) {
        return [IntPtr]::Zero
    }

    return $found[$found.Count - 1]
}

if ($RestartConnection) {
    Get-Process vmconnect -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

$hwnd = Find-VmConnectWindow -Name $VmName
if ($hwnd -eq [IntPtr]::Zero) {
    $vmconnect = Join-Path $env:WINDIR "System32\vmconnect.exe"
    Start-Process -FilePath $vmconnect -ArgumentList @("localhost", $VmName)
    for ($i = 0; $i -lt 30 -and $hwnd -eq [IntPtr]::Zero; $i++) {
        Start-Sleep -Milliseconds 300
        $hwnd = Find-VmConnectWindow -Name $VmName
    }
}

if ($hwnd -eq [IntPtr]::Zero) {
    throw "Could not find connected VMConnect window for VM: $VmName"
}

[void][VmConnectInput]::ShowWindow($hwnd, [VmConnectInput]::SW_RESTORE)
[void][VmConnectInput]::SetWindowPos(
    $hwnd,
    [VmConnectInput]::HWND_TOPMOST,
    0,
    0,
    0,
    0,
    [VmConnectInput]::SWP_NOMOVE -bor [VmConnectInput]::SWP_NOSIZE -bor [VmConnectInput]::SWP_SHOWWINDOW
)
[void][VmConnectInput]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds $InitialDelayMs

$rect = New-Object RECT
[void][VmConnectInput]::GetWindowRect($hwnd, [ref]$rect)
Write-Log "Window rect: left=$($rect.Left), top=$($rect.Top), right=$($rect.Right), bottom=$($rect.Bottom)"

if ($ClickSpec.Trim().Length -gt 0) {
    foreach ($pair in $ClickSpec.Split(";")) {
        $parts = $pair.Split(",")
        if ($parts.Count -ne 2) {
            throw "Invalid click pair: $pair"
        }

        $x = $rect.Left + [int]$parts[0]
        $y = $rect.Top + [int]$parts[1]
        Write-Log "Click relative=$pair absolute=$x,$y"
        [void][VmConnectInput]::SetCursorPos($x, $y)
        Start-Sleep -Milliseconds 80
        [VmConnectInput]::mouse_event([VmConnectInput]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 80
        [VmConnectInput]::mouse_event([VmConnectInput]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds $DelayAfterClickMs
    }
}

Start-Sleep -Milliseconds $FinalDelayMs

[void][VmConnectInput]::SetWindowPos(
    $hwnd,
    [VmConnectInput]::HWND_NOTOPMOST,
    0,
    0,
    0,
    0,
    [VmConnectInput]::SWP_NOMOVE -bor [VmConnectInput]::SWP_NOSIZE -bor [VmConnectInput]::SWP_SHOWWINDOW
)

Write-Log "Done"
