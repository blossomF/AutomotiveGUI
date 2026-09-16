"""让 pytest 从项目根目录运行时可以导入 ``autogui`` 包。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
