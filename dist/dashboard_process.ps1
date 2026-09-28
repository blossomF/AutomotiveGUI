param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'start', 'stop')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root 'server\autogui\.venv\Scripts\python.exe'
$worker = Join-Path $root 'dist\dashboard_background.bat'
$pidFile = Join-Path $root '.dashboard.pid'
$logFile = Join-Path $root '.dashboard.log'
$errorLog = Join-Path $root '.dashboard.log.err'

function Get-DashboardProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $parts = (Get-Content -LiteralPath $pidFile -Raw).Trim().Split('|')
    $idNumber = 0
    if (-not [int]::TryParse($parts[0], [ref]$idNumber) -or $idNumber -le 0) { return $null }
    if ($parts.Count -eq 1) {
        # 兼容旧 start.bat 留下的纯数字 PID；只识别确实监听面板端口的 Python。
        $legacy = Get-Process -Id $idNumber -ErrorAction SilentlyContinue
        if (-not $legacy) { return $null }
        if (-not [string]::Equals($legacy.Path, $python,
                [StringComparison]::OrdinalIgnoreCase)) { return $null }
        $listener = Get-NetTCPConnection -LocalPort 8777 -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $idNumber }
        if ($listener) { return $legacy }
        throw "Old dashboard PID $idNumber is running but could not be verified; stop it manually before restarting."
    }
    if ($parts.Count -ne 2) { return $null }
    $startTicks = [long]0
    if (-not [long]::TryParse($parts[1], [ref]$startTicks)) { return $null }
    $process = Get-Process -Id $idNumber -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if (-not [string]::Equals($process.Path, $env:ComSpec,
            [StringComparison]::OrdinalIgnoreCase)) { return $null }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $startTicks) { return $null }
    return $process
}

try {
    if ($Action -eq 'status') {
        if (Get-DashboardProcess) { exit 0 }
        exit 1
    }

    if ($Action -eq 'start') {
        if (Get-DashboardProcess) { throw 'Dashboard is already running; use stop.bat first.' }
        if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
        if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
        $argument = '/d /s /c ' + [char]34 + [char]34 + $worker + [char]34 + [char]34
        $process = Start-Process -FilePath $env:ComSpec -ArgumentList $argument `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru
        $identity = "$($process.Id)|$($process.StartTime.ToUniversalTime().Ticks)"
        Set-Content -LiteralPath $pidFile -Value $identity -Encoding Ascii -NoNewline
        Start-Sleep -Milliseconds 1200
        if (-not (Get-DashboardProcess)) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
            throw "Dashboard exited during startup; see $errorLog and $logFile"
        }
        Write-Host "[OK] Dashboard PID: $($process.Id)"
        exit 0
    }

    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Host '[INFO] No background dashboard PID file.'
        exit 0
    }
    $process = Get-DashboardProcess
    if (-not $process) {
        Remove-Item -LiteralPath $pidFile -Force
        Write-Host '[INFO] Removed stale PID file; no matching dashboard was stopped.'
        exit 0
    }
    & taskkill.exe /F /T /PID $process.Id >$null 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Failed to stop dashboard PID $($process.Id)" }
    Start-Sleep -Milliseconds 300
    if (Get-DashboardProcess) { throw "Dashboard PID $($process.Id) is still running" }
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host "[OK] Stopped background dashboard PID $($process.Id)"
    exit 0
} catch {
    Write-Error "[FAIL] $($_.Exception.Message)"
    exit 1
}
