"""支持 ``python -m leakcheck``。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
