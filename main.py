"""PIP 包管理器 GUI 入口。
    v1.0
    python main.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipmgr.ui import main  # noqa: E402

if __name__ == "__main__":
    main()
