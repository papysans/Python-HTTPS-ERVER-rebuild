#!/usr/bin/env bash
# 原 run-tests.sh 的 shebang 写死 /usr/bin/bash，该路径在 macOS 上不存在。
set -euo pipefail
cd "$(dirname "$0")"

# 单元测试：不起进程、不绑端口，可并行
python3 -m pytest tests/test_unit.py -v

# 功能测试：进程内起服务，端口由内核分配（port=0），不争抢 8080
python3 -m pytest tests/test_functional.py -v
