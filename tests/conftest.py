import sys
from pathlib import Path

# 让测试能 import 被测模块（原代码只能靠 subprocess 拉起进程来测）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
