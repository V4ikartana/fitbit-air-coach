"""macOS user notifications."""
from __future__ import annotations

import subprocess
from typing import Callable


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, run: Callable = subprocess.run) -> None:
    script = f"display notification {_quote(message)} with title {_quote(title)}"
    run(["osascript", "-e", script], check=False, capture_output=True)
