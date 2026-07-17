#Requires -Version 5
# Windows 下运行全部测试，等价于 run-tests.sh。
# 用法（在本目录下）：
#   powershell -ExecutionPolicy Bypass -File .\run-tests.ps1
# 或先 Set-ExecutionPolicy -Scope Process Bypass 再 .\run-tests.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Windows 上 Python 可能叫 python / py / python3，按顺序探测第一个可用的。
$python = $null
foreach ($candidate in @("python", "py", "python3")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    Write-Error "未找到 Python 解释器（尝试过 python / py / python3）。请先安装 Python 3.10+。"
    exit 1
}

# 单元测试：不起进程、不绑端口，可并行
& $python -m pytest tests/test_unit.py -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 功能测试：进程内起服务，端口由内核分配（port=0），不争抢 8080
& $python -m pytest tests/test_functional.py -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
