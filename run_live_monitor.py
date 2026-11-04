"""Start the live-only CareMate viewer from any working directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))

from vision.live_monitor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
