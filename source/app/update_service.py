from __future__ import annotations

"""Small, dependency-free client for the portable Windows updater.

The application only reads a public JSON manifest.  The manifest points to a
versioned ZIP file and includes its SHA-256 digest.  Actual replacement is
performed by the separate GupiaoUpdater.exe process so the running program is
never asked to overwrite itself.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import base64
import hashlib
import shutil
import time
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from update_net import open_url


CURRENT_VERSION = "1.6.2"
MANIFEST_TIMEOUT_SECONDS = 8.0
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/yefeng1346/gupiao-updates/main/latest.json"
MANIFEST_FALLBACK_URL = "https://api.github.com/repos/yefeng1346/gupiao-updates/contents/latest.json?ref=main"


class UpdateError(ValueError):
    """A user-facing update configuration or network error."""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    url: str
    sha256: str
    notes: str = ""
    published_at: str = ""
    urls: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
            "notes": self.notes,
            "published_at": self.published_at,
            "urls":list(self.urls),
        }


def _version_tuple(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise UpdateError(f"版本号格式不正确：{value}")
    return tuple(int(part) for part in parts)


def is_newer_version(candidate: str, current: str = CURRENT_VERSION) -> bool:
    candidate_parts = _version_tuple(candidate)
    current_parts = _version_tuple(current)
    width = max(len(candidate_parts), len(current_parts))
    return candidate_parts + (0,) * (width - len(candidate_parts)) > current_parts + (
        0,
    ) * (width - len(current_parts))


def _validate_https_url(value: Any, field_name: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"https"} or not parsed.netloc:
        raise UpdateError(f"更新清单中的 {field_name} 必须是 HTTPS 地址")
    return url


def parse_manifest(payload: Any) -> UpdateManifest:
    if not isinstance(payload, dict):
        raise UpdateError("更新清单不是 JSON 对象")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise UpdateError("更新清单缺少 version")
    _version_tuple(version)
    url = _validate_https_url(payload.get("url"), "url")
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise UpdateError("更新清单中的 sha256 必须是 64 位十六进制字符串")
    urls = payload.get("urls",[])
    if not isinstance(urls,list) or len(urls)>5:
        raise UpdateError("备用下载地址格式错误")
    return UpdateManifest(
        version=version.lstrip("v"),
        url=url,
        sha256=sha256,
        notes=str(payload.get("notes") or "").strip(),
        published_at=str(payload.get("published_at") or "").strip(),
        urls=tuple(_validate_https_url(value,"备用下载地址") for value in urls),
    )


def fetch_manifest(manifest_url: str) -> UpdateManifest:
    url = _validate_https_url(manifest_url, "manifest_url")
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "GupiaoStockTool-Updater/1.1",
    }
    last_error: Exception | None = None
    raw = b""
    urls = [url]
    if url == DEFAULT_MANIFEST_URL: urls.append(MANIFEST_FALLBACK_URL)
    deadline = time.monotonic()+30
    for endpoint in urls:
        try:
            response = open_url(endpoint,headers,timeout=MANIFEST_TIMEOUT_SECONDS,deadline=deadline)
            with response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw)>1024*1024: raise UpdateError("更新清单过大")
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload,dict) and payload.get("encoding")=="base64" and payload.get("type")=="file":
                payload = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            return parse_manifest(payload)
        except Exception as exc:  # pragma: no cover - depends on the user's network
            last_error = exc
    raise UpdateError(f"读取更新清单失败（{len(urls)}个地址，代理/直连）：{type(last_error).__name__}: {last_error}") from last_error


def promote_bundled_updater(target_dir, attempts=30):
    """Old updaters skip their own EXE; new app installs a bundled successor."""
    target_dir = Path(target_dir).resolve()
    source = target_dir/"_internal"/"GupiaoUpdater.next.exe"
    target = target_dir/"GupiaoUpdater.exe"
    if not source.is_file(): return False
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream,"sha256").digest()
    if target.is_file():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream,"sha256").digest()==digest:
                return True
    scratch = target_dir/f"GupiaoUpdater-{os.getpid()}.new"
    try:
        for _ in range(attempts):
            try:
                shutil.copy2(source,scratch)
                os.replace(scratch,target)
                return True
            except OSError:
                time.sleep(1)
        return False
    finally:
        try: scratch.unlink(missing_ok=True)
        except OSError: pass


class UpdatePreparation:
    """Keep software open until a complete verified archive is ready."""
    def __init__(self):
        self.lock,self.cancel_event = Lock(),Event()
        self.state = {"status":"idle"}
        self.archive,self.manifest = None,None
        self.worker = None

    def snapshot(self):
        with self.lock: return dict(self.state)

    def start(self,manifest):
        with self.lock:
            if self.state["status"] in {"downloading","ready","installing"}: return dict(self.state)
            self.cancel_event = Event()
            self.manifest = manifest
            self.state = {"status":"downloading","version":manifest.version,"bytes":0,"total":0,"message":"正在下载，当前软件仍可使用"}
        def progress(count,total,attempt,sources):
            with self.lock: self.state.update(bytes=count,total=total,attempt=attempt,sources=sources)
        def work():
            import updater_app
            cache = Path(tempfile.gettempdir())/"GupiaoStockTool-update"/manifest.sha256
            archive = cache/"update.zip"
            lock_file = None
            try:
                cache.mkdir(parents=True,exist_ok=True)
                lock_file = (cache/"download.lock").open("a+b")
                if os.name=="nt":
                    import msvcrt
                    lock_file.seek(0)
                    try: msvcrt.locking(lock_file.fileno(),msvcrt.LK_NBLCK,1)
                    except OSError as exc: raise UpdateError("另一个窗口正在下载相同更新，请稍后重试") from exc
                updater_app._download(manifest.url,manifest.sha256,archive,urls=manifest.urls,progress=progress,cancel=self.cancel_event)
                if self.cancel_event.is_set(): raise updater_app.DownloadCancelled("已取消")
                # Validate the archive layout before telling UI it is ready.
                with tempfile.TemporaryDirectory(prefix="gupiao-update-validate-") as folder:
                    updater_app._safe_extract(archive,Path(folder))
                with self.lock:
                    self.archive = archive
                    self.state.update(status="ready",message="下载及校验完成，准备重启安装")
            except Exception as exc:
                with self.lock: self.state.update(status="cancelled" if self.cancel_event.is_set() else "failed",message=str(exc))
            finally:
                if lock_file: lock_file.close()
        self.worker = Thread(target=work,daemon=True,name="gupiao-update-download")
        self.worker.start()
        return self.snapshot()

    def cancel(self):
        self.cancel_event.set()
        with self.lock:
            if self.state["status"]=="ready": self.state.update(status="cancelled",message="已取消安装，原版本继续使用")
        return self.snapshot()

    def install(self,target_dir,parent_pid):
        import updater_app
        if not getattr(sys,"frozen",False): raise UpdateError("开发模式不能安装桌面更新")
        with self.lock:
            if self.state["status"]!="ready" or self.cancel_event.is_set(): raise UpdateError("安装包尚未准备好或已取消")
            if updater_app.other_install_processes(Path(target_dir),parent_pid):
                raise UpdateError("还有同一目录的软件窗口在运行，请关闭其他窗口后再安装")
            # Launch a temporary copy, so installing a newer updater cannot
            # overwrite or lock the updater process executing this install.
            bundled = Path(target_dir)/"_internal"/"GupiaoUpdater.next.exe"
            source = bundled if bundled.is_file() else Path(target_dir)/"GupiaoUpdater.exe"
            helper_dir = Path(tempfile.mkdtemp(prefix="gupiao-installer-"))
            helper = helper_dir/"GupiaoUpdater.exe"
            shutil.copy2(source,helper)
            command = [str(helper),"--archive",str(self.archive),"--sha256",self.manifest.sha256,"--target-dir",str(Path(target_dir).resolve()),"--parent-pid",str(parent_pid),"--restart",str(Path(target_dir).resolve()/"GupiaoStockTool.exe")]
            flags = getattr(subprocess,"DETACHED_PROCESS",0)|getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)
            subprocess.Popen(command,cwd=str(target_dir),close_fds=True,creationflags=flags,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.state.update(status="installing",message="正在重启安装，配置和用户数据保留")
        return self.snapshot()


UPDATE_PREPARATION = UpdatePreparation()


def get_update_info(manifest_url: str) -> dict[str, Any]:
    """Return a JSON-safe status payload for the API and web UI."""
    configured = bool(str(manifest_url or "").strip())
    result: dict[str, Any] = {
        "configured": configured,
        "current_version": CURRENT_VERSION,
        "available": False,
        "update": None,
        "error": "",
    }
    if not configured:
        return result
    try:
        manifest = fetch_manifest(manifest_url)
        result["available"] = is_newer_version(manifest.version)
        result["update"] = manifest.as_dict()
    except UpdateError as exc:
        result["error"] = str(exc)
    return result


def launch_updater(manifest_url: str, target_dir: Path, parent_pid: int) -> None:
    """Start the packaged updater and return before the main process exits."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("开发模式不能执行桌面版更新，请使用打包后的 EXE 测试")
    target_dir = Path(target_dir).resolve()
    updater = target_dir / "GupiaoUpdater.exe"
    if not updater.exists():
        raise UpdateError("当前版本缺少 GupiaoUpdater.exe，请先安装支持在线更新的新版本")
    if not str(manifest_url or "").strip():
        raise UpdateError("尚未配置 UPDATE_MANIFEST_URL")

    work_dir = Path(tempfile.gettempdir()) / "GupiaoStockTool-update"
    work_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(updater),
        "--manifest-url",
        str(manifest_url).strip(),
        "--target-dir",
        str(target_dir),
        "--parent-pid",
        str(int(parent_pid)),
        "--restart",
        str(target_dir / "GupiaoStockTool.exe"),
    ]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(
            command,
            cwd=str(target_dir),
            close_fds=True,
            creationflags=creation_flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateError(f"无法启动更新程序：{exc}") from exc
