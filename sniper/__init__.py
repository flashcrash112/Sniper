"""A latency-focused pump.fun launch sniper."""

import sys

# Checked here so an old interpreter produces an explanation rather than a
# `ModuleNotFoundError: tomllib` from deep inside the config loader. Ubuntu
# 22.04 ships Python 3.10, which is a realistic way to land here.
if sys.version_info < (3, 11):
    raise RuntimeError(
        f"This bot needs Python 3.11 or newer; you are running "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        f"On Ubuntu/WSL: sudo apt install python3.12 python3.12-venv, then "
        f"rebuild the virtualenv with: python3.12 -m venv .venv"
    )

__version__ = "0.1.0"
