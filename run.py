"""Start the ArtDrop server and open the browser.

If the port is already in use, assume ArtDrop is running and just open the
browser. Keep the console window open while you post.
"""
from __future__ import annotations

import os
import socket
import sys
import webbrowser

from app.core.paths import HOST, PORT, URL


def already_running() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((HOST, PORT)) == 0


def main() -> int:
    if already_running():
        print(f"[ArtDrop] Already running — opening {URL}")
        webbrowser.open(URL)
        return 0

    os.environ["ARTDROP_OPEN_BROWSER"] = "1"
    import uvicorn

    print(f"[ArtDrop] Starting at {URL} — keep this window open while you post.")
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
