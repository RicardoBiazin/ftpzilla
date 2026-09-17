"""Ponto de entrada do FTPZilla (alvo do PyInstaller)."""
from __future__ import annotations

import sys

from ftpzilla.cli import main

if __name__ == "__main__":
    sys.exit(main())
