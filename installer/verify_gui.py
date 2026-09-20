"""Launch the real frozen Windows entry point and require a responsive Qt window.

Run from an empty working directory with no development tools on PATH. Save a screenshot
of the actual window, then close only the process this check started. No API calls.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab


def verify(executable: Path, screenshot: Path, timeout: float = 30) -> dict[str, object]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    env = os.environ.copy()
    windows = Path(env["SYSTEMROOT"])
    env["PATH"] = os.pathsep.join(map(str, [windows / "System32", windows]))
    for name in list(env):
        if name.upper().startswith(("PYTHON", "QT_", "QML")):
            env.pop(name)
    env["QT_QPA_PLATFORM"] = "windows"
    executable, screenshot = executable.resolve(), screenshot.resolve()
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    with tempfile.TemporaryDirectory(prefix="spice-gui-check-") as cwd:
        process = subprocess.Popen([str(executable)], cwd=cwd, env=env)
        started = time.monotonic()
        try:
            while time.monotonic() - started < timeout:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Application exited before opening its window: {process.returncode}"
                    )
                windows_found = []

                @callback_type
                def collect(hwnd, _, found=windows_found):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == process.pid and user32.IsWindowVisible(hwnd):
                        title, kind = (
                            ctypes.create_unicode_buffer(512),
                            ctypes.create_unicode_buffer(256),
                        )
                        user32.GetWindowTextW(hwnd, title, len(title))
                        user32.GetClassNameW(hwnd, kind, len(kind))
                        found.append((hwnd, title.value, kind.value))
                    return True

                user32.EnumWindows(collect, 0)
                for hwnd, title, kind in windows_found:
                    if "Unhandled exception" in title or "Error" in title:
                        user32.PostMessageW(hwnd, 0x0010, 0, 0)
                        raise RuntimeError(f"Application opened an error dialog: {title}")
                    if title.startswith("Spice Maker") and kind.startswith("Qt"):
                        handle = hwnd
                        user32.ShowWindow(hwnd, 9)  # Restore the test-owned window.
                        user32.SetForegroundWindow(hwnd)
                        user32.RedrawWindow(hwnd, None, None, 0x185)
                        time.sleep(1)
                        reply = ctypes.c_size_t()
                        if not user32.SendMessageTimeoutW(
                            hwnd, 0, 0, 0, 2, 3000, ctypes.byref(reply)
                        ):
                            raise RuntimeError("The application window is not responding")
                        ImageGrab.grab(window=hwnd).save(screenshot)
                        user32.PostMessageW(hwnd, 0x0010, 0, 0)
                        code = process.wait(timeout=5)
                        if code != 0:
                            raise RuntimeError(f"Application exited with {code}")
                        return {
                            "status": "PASS",
                            "title": title,
                            "window_class": kind,
                            "seconds": round(time.monotonic() - started, 3),
                            "screenshot": str(screenshot),
                            "executable": str(executable),
                        }
                time.sleep(0.1)
            raise TimeoutError("No visible Spice Maker Qt window appeared")
        finally:
            if process.poll() is None:
                if handle:
                    user32.PostMessageW(handle, 0x0010, 0, 0)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.executable, args.screenshot), indent=2))
