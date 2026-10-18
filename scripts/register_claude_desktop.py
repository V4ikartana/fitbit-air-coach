"""Register the fitbit-coach MCP server in Claude Desktop, then reopen Claude.

Claude Desktop rewrites claude_desktop_config.json from memory while it runs, so this must run with Claude fully
quit (Cmd+Q). Usage, from Terminal:  FITBIT_AIR_HOME=~/.fitbit-air python3 <repo>/scripts/register_claude_desktop.py
When FITBIT_AIR_HOME is set, the entry passes it to the server, so the coach reads that home.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
REPO = Path(__file__).resolve().parents[1]
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def build_entry(environ=os.environ) -> dict:
    entry = {"command": UV, "args": ["--directory", str(REPO), "run", "--frozen", "fitbit-air", "mcp"]}
    if environ.get("FITBIT_AIR_HOME"):
        entry["env"] = {"FITBIT_AIR_HOME": str(Path(environ["FITBIT_AIR_HOME"]).expanduser().resolve())}
    return entry


APP_BINARY = "/Claude.app/Contents/MacOS/Claude"


def claude_running() -> bool:
    out = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True).stdout
    return any(line.strip().endswith(APP_BINARY) for line in out.splitlines())


def open_claude() -> None:
    subprocess.run(["open", "-a", "Claude"])


def main() -> int:
    if claude_running():
        print("Claude Desktop is still running. Quit it with Cmd+Q (not just closing the window), then run this again.")
        return 1
    entry = build_entry()
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if config.get("mcpServers", {}).get("fitbit-coach") == entry:  # includes env, so a new home counts
        print("fitbit-coach is already registered.")
    else:
        if CONFIG.exists():
            backup = CONFIG.with_name(f"{CONFIG.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(CONFIG, backup)
            print(f"Backed up the old config to {backup}")
        config.setdefault("mcpServers", {})["fitbit-coach"] = entry
        CONFIG.write_text(json.dumps(config, indent=2))
        home = f" (FITBIT_AIR_HOME={entry['env']['FITBIT_AIR_HOME']})" if "env" in entry else ""
        print(f"Registered fitbit-coach: {UV} {' '.join(entry['args'])}{home}")
    open_claude()
    print("Reopening Claude Desktop. In a new chat, look for fitbit-coach under the + / tools menu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
