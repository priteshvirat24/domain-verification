"""Backward-compatible local command: use `python -m verifier` in new jobs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verifier.cli import main

if __name__ == "__main__":
    main()
