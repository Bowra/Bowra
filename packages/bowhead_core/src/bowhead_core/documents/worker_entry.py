"""Launch the baked worker with Python isolated mode and a trusted import root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bowhead_core.documents.worker import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
