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

    class Api:
        """Exposed to the widget page so its title-bar buttons can control
        the frameless window (minimize / pin / open full dashboard)."""

        def __init__(self, port):
            self.window = None
            self.on_top = True
            self.port = port
            self.dash = None

        def minimize(self):
            try:
                self.window.minimize()
            except Exception:
                pass

        def toggle_top(self):
            self.on_top = not self.on_top
            try:
                self.window.on_top = self.on_top
            except Exception:
                pass
            return self.on_top

        def open_dashboard(self):
            # full HUD in its own normal, maximized window
            try:
                self.dash = webview.create_window(
                    "Jarvis Dashboard",
                    url=f"http://localhost:{self.port}/",
                    width=1280,
                    height=800,
                    maximized=True,
                )
            except Exception:
                try:
                    self.dash = webview.create_window(
                        "Jarvis Dashboard",
                        url=f"http://localhost:{self.port}/",
                        width=1280,
                        height=800,
                    )
                except Exception:
                    pass

    api = Api(args.port)

    # Start at a guaranteed-visible spot; dock() refines to the right edge
    # after start using pywebview's own screen units, clamped fully on-screen.
    window = webview.create_window(
        "Jarvis",
        url=url,
        width=args.width,
        height=args.height,
        x=120,
        y=120,
        frameless=True,
        easy_drag=True,
        on_top=True,
        resizable=True,
        background_color="#04080e",
        js_api=api,
    )
    api.window = window

    def dock():
        try:
            scr = webview.screens[0]
            sw2, sh2 = int(scr.width), int(scr.height)
            nx = (sw2 - args.width - args.margin) if args.side == "right" else args.margin
            ny = max(40, (sh2 - args.height) // 2 - 40)
            # never let any part fall off the screen
            nx = max(0, min(nx, sw2 - args.width))
            ny = max(0, min(ny, sh2 - args.height))
            window.move(nx, ny)
        except Exception:
            pass

    webview.start(dock)


if __name__ == "__main__":
    main()
