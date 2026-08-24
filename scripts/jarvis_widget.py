"""Jarvis desktop widget — a frameless, always-on-top mini-panel docked to the
side of the screen, showing the live heartbeat core + status. No browser tab.

Runs as its own small process (the voice backend stays hidden). It just points
a native webview at the local dashboard's /widget page.

    uv pip install pywebview
    uv run python jarvis_widget.py            # right-docked
    uv run python jarvis_widget.py --side left
    uv run python jarvis_widget.py --width 360 --height 560
"""

from __future__ import annotations

import argparse
import sys


def screen_size() -> tuple[int, int]:
    if sys.platform == "win32":
        import ctypes

        u = ctypes.windll.user32
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    return 1920, 1080


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--width", type=int, default=340)
    ap.add_argument("--height", type=int, default=560)
    ap.add_argument("--side", choices=["right", "left"], default="right")
    ap.add_argument("--margin", type=int, default=24)
    args = ap.parse_args()

    try:
        import webview
    except Exception:
        print("[widget] pywebview not installed. Run:  uv pip install pywebview")
        sys.exit(1)

    # Wait for the backend's web server to be reachable before opening the
    # window — otherwise pywebview loads a blank/black page and never retries.
    import time
    import urllib.request

    url = f"http://localhost:{args.port}/widget"
    for _ in range(90):  # up to ~90s
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)

    # rough initial placement (ctypes) — refined after start using pywebview's
    # own screen units so DPI scaling can't push it off to the wrong corner
    sw, sh = screen_size()
    y = max(40, (sh - args.height) // 2 - 40)
    x = (sw - args.width - args.margin) if args.side == "right" else args.margin

    window = webview.create_window(
        "Jarvis",
        url=url,
        width=args.width,
        height=args.height,
        x=x,
        y=y,
        frameless=True,
        easy_drag=True,
        on_top=True,
        resizable=True,
        background_color="#04080e",
    )

    def dock():
        try:
            scr = webview.screens[0]
            sw2, sh2 = int(scr.width), int(scr.height)
            nx = (sw2 - args.width - args.margin) if args.side == "right" else args.margin
            ny = max(40, (sh2 - args.height) // 2 - 40)
            window.move(nx, ny)
        except Exception:
            pass

    webview.start(dock)


if __name__ == "__main__":
    main()
