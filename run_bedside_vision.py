"""Start bedside vision from any working directory; --help has no side effects."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))

from vision.bedside_watch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
