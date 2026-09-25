# -*- coding: utf-8 -*-
# dsh-pet-standalone 启动脚本（源码方式）：开始菜单快捷方式指向本文件。
# 行为：切到仓库根目录 -> 用系统 Python 进入 pet 模块；已有一个桌宠进程
# 在跑时不重复启动（桌面宠重复开只会在碰撞 IPC 里当第二实例）。
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级 = 仓库根
Set-Location $root

# 已有实例在跑就不再拉起（-m pet 的 python 进程）
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '-m pet' }
if ($existing) { exit 0 }

# PYTHONUTF8：与打包构建同纪律（issue #26），避免 GBK 代码页读 UTF-8 资源乱码
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# 优先仓库 venv（依赖齐全），没有才退系统 Python
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'C:\Python\Python311\python.exe' }

Start-Process -WindowStyle Hidden -FilePath $python `
    -ArgumentList '-m', 'pet' `
    -WorkingDirectory $root
