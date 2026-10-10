from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from main import app


HOST = "127.0.0.1"
WINDOW_TITLE = "本地股票板块复盘工具"


def _console_print(*values: object) -> None:
    """Print only when a console is available (PyInstaller windowed builds have none)."""
    if sys.stdout is not None:
        try:
            print(*values, flush=True)
        except OSError:
            # PyInstaller's windowed executable can expose an invalid standard
            # handle when a smoke test finishes; logging must not turn success
            # into a startup error.
            pass


def _startup_error_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "desktop-startup-error.log"
    return Path(__file__).resolve().parent / "desktop-startup-error.log"


def _show_startup_error(exc: BaseException) -> None:
    error_path = _startup_error_path()
    try:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            WINDOW_TITLE,
            f"桌面工具启动失败。\n详细错误已写入：{error_path}\n\n{exc}",
        )
        root.destroy()
    except Exception:
        _console_print(f"桌面工具启动失败，详细错误已写入：{error_path}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _service_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}/api/health", timeout=1.5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("ok") is True and payload.get("service") == "gupiao"
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _start_api() -> tuple[uvicorn.Server, threading.Thread, int]:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="gupiao-api", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _service_ready(port):
            return server, thread, port
        if not thread.is_alive():
            break
        time.sleep(0.2)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("本地行情服务启动失败，请检查 desktop-startup-error.log")


def _stop_api(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def run_smoke_test() -> int:
    """Start and stop the embedded API without opening a GUI window."""
    server, thread, port = _start_api()
    try:
        _console_print(f"desktop api ready: http://{HOST}:{port}/")
        # Exercise the real install boundary in every packaged smoke test.
        # A fresh process has no prepared archive: it must reject safely, not
        # raise HTTP 500. This never downloads or launches an installer.
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/update/status", timeout=5) as response:
            assert json.loads(response.read())["status"] == "idle"
        request = urllib.request.Request(f"http://{HOST}:{port}/api/update/install", data=b"", method="POST")
        try:
            urllib.request.urlopen(request, timeout=5).close()
            raise RuntimeError("更新安装接口在没有准备安装包时未拒绝请求")
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())
            if exc.code != 409 or not payload.get("detail"):
                raise RuntimeError(f"更新安装接口冒烟测试失败：HTTP {exc.code}") from exc
        return 0
    finally:
        _stop_api(server, thread)
        _console_print("desktop api stopped")


def run_desktop() -> int:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "缺少 pywebview，请先运行 start_desktop.bat 安装依赖"
        ) from exc

    server, thread, port = _start_api()
    closed = threading.Event()

    def close_api() -> None:
        if closed.is_set():
            return
        closed.set()
        _stop_api(server, thread)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://{HOST}:{port}/",
        width=1440,
        height=960,
        min_size=(1100, 700),
        resizable=True,
    )
    window.events.closed += close_api
    try:
        webview.start(debug=False)
    finally:
        close_api()
    return 0


def main() -> int:
    if getattr(sys,"frozen",False):
        from app.update_service import promote_bundled_updater
        threading.Thread(target=promote_bundled_updater,args=(Path(sys.executable).resolve().parent,),daemon=True,name="updater-compatibility").start()
    parser = argparse.ArgumentParser(description=WINDOW_TITLE)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="启动并关闭内嵌服务，用于验证桌面启动器",
    )
    args = parser.parse_args()
    return run_smoke_test() if args.smoke_test else run_desktop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _show_startup_error(exc)
        raise SystemExit(1) from exc
