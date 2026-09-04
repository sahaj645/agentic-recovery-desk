#!/usr/bin/env python3
"""Zero-install entry point.

    python run.py demo
    python run.py eval

The Makefile calls this, and so can anyone without make on their PATH. It puts
``src`` on the path itself so the repository runs straight after a clone, with
no install step and no virtualenv.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from recovery_desk.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
