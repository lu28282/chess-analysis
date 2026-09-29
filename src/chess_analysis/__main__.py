"""Allow `python -m chess_analysis` (used by the web UI job runner)."""

from .cli import app

if __name__ == "__main__":
    app()
