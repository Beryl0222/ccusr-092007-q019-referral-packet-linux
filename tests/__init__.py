"""测试包初始化：保证 unittest discover 下 src 在 sys.path 最前。"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
