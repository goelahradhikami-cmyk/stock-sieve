"""pytest 配置：将项目根目录加入 sys.path，使 `src` 包可被测试导入。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
