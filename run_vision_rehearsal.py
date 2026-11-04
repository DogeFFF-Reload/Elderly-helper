"""Run the offline vision demo from any working directory."""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))

from vision.rehearsal import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
