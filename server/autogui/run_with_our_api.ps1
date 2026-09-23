# 用「我们的」ThunderSoft 官方 API + 交互面板跑队友的 AutomotiveGUI 框架。
# 关键：绝不把 key 写进队友框架文件——运行时从 LightGUIAgent/.env 读取并注入环境变量。
# 默认行为：启动 dist/dashboard_server.py（交互网址 http://localhost:8777），
#          从浏览器下指令；面板会带着 GLM_API_KEY 环境启动 main.py 跑任务。
# 用法（在 server\autogui 目录执行）：
#    .\run_with_our_api.ps1                 # 启动交互面板（dist/dashboard_server.py），浏览器打开 http://localhost:8777
#    .\run_with_our_api.ps1 -Task "打开车机设置"   # 不走面板，直接跑一条任务
# 注意：首次使用需先为队友框架建好依赖（pip install -e . 或 uv sync）。

param([string]$Task = "")

$ErrorActionPreference = 'Stop'
$partnerDir = $PSScriptRoot
$ourEnv = "C:\Users\CART\Desktop\load_test\LightGUIAgent\.env"

# —— 从我们的 .env 读取 ThunderSoft key（OPENAI_API_KEY 即官方 key） ——
$key = $null
if (Test-Path $ourEnv) {
    foreach ($line in (Get-Content $ourEnv)) {
        if ($line -match '^\s*OPENAI_API_KEY\s*=\s*(.+?)\s*$') {
            $key = $Matches[1].Trim().Trim('"').Trim("'")
            break
        }
    }
}
if (-not $key) {
    Write-Error "未在 $ourEnv 找到 OPENAI_API_KEY，请先确认 LightGUIAgent 的 .env 存在且含官方 key。"
    exit 1
}

# 注入到进程环境（队友框架 config.py 读 GLM_API_KEY）
$env:GLM_API_KEY   = $key
$env:OPENAI_API_KEY = $key   # 双保险，部分依赖可能读这个名

Set-Location $partnerDir

if ($Task) {
    Write-Host "[run] 直接跑任务: $Task" -ForegroundColor Cyan
    Write-Host "[run] GLM_API_KEY 已注入（长度 $($key.Length)）" -ForegroundColor Cyan
    python main.py $Task
} else {
    Write-Host "[run] 启动交互面板 (http://localhost:8777)，从浏览器下指令" -ForegroundColor Cyan
    Write-Host "[run] GLM_API_KEY 已注入（长度 $($key.Length)），端点见 config.yaml: glm.base_url" -ForegroundColor Cyan
    Start-Process "http://localhost:8777"
    python ../dist/dashboard_server.py
}
