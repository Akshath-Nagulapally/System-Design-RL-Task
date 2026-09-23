"""Entry point for a job-scoped deployment server process."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import main

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected job id")
    main()
