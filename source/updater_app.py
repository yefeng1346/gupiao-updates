from __future__ import annotations

"""Standalone updater bundled beside GupiaoStockTool.exe.

It intentionally depends only on the Python standard library so it can run
after the main executable has exited and while the application's _internal
directory is being replaced.
"""

import argparse
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import zipfile
from update_net import open_url


APP_NAME = "本地股票板块复盘工具"
DOWNLOAD_TIMEOUT_SECONDS = 15.0
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAYS = (2.0, 5.0, 10.0)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
PRESERVED_NAMES = {".env", "data", "reports", "update-backups"}
SKIP_UPDATE_NAMES = {"GupiaoUpdater.exe", "desktop-startup-error.log", "update-error.log"}


class DownloadCancelled(RuntimeError):
    pass


def _message(title: str, message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _log_path(target_dir: Path) -> Path:
    return target_dir / "update-error.log"


def _write_error(target_dir: Path, message: str) -> None:
    try:
        _log_path(target_dir).write_text(message, encoding="utf-8")
    except OSError:
        pass


def _wait_for_parent(pid: int) -> None:
    if pid <= 0 or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes
    kernel32.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if handle:
        try:
            result = kernel32.WaitForSingleObject(handle, 120000)
            if result == 0x102:
                raise RuntimeError("等待主程序退出超时，更新已取消")
        finally:
            kernel32.CloseHandle(handle)
        return
    # The process may already have exited between Popen and this call.
    time.sleep(0.5)


def other_install_processes(target_dir: Path, parent_pid: int) -> list[int]:
    """Check exact executable path, not unrelated installs or process names."""
    if os.name!="nt": return []
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize",w.DWORD),("cntUsage",w.DWORD),("th32ProcessID",w.DWORD),("th32DefaultHeapID",ctypes.c_size_t),("th32ModuleID",w.DWORD),("cntThreads",w.DWORD),("th32ParentProcessID",w.DWORD),("pcPriClassBase",w.LONG),("dwFlags",w.DWORD),("szExeFile",w.WCHAR*260)]
    dll = ctypes.WinDLL("kernel32",use_last_error=True)
    dll.CreateToolhelp32Snapshot.argtypes = [w.DWORD,w.DWORD]; dll.CreateToolhelp32Snapshot.restype = w.HANDLE
    dll.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]; dll.OpenProcess.restype = w.HANDLE
    dll.QueryFullProcessImageNameW.argtypes = [w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    dll.Process32FirstW.argtypes = [w.HANDLE,ctypes.POINTER(Entry)]
    dll.Process32NextW.argtypes = [w.HANDLE,ctypes.POINTER(Entry)]
    dll.CloseHandle.argtypes = [w.HANDLE]
    snapshot = dll.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value: raise RuntimeError("无法确认其他窗口状态，未安装")
    entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
    expected = os.path.normcase(str((target_dir/"GupiaoStockTool.exe").resolve()))
    found = []
    try:
        ok = dll.Process32FirstW(snapshot,ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID!=parent_pid and entry.szExeFile.lower()=="gupiaostocktool.exe":
                process = dll.OpenProcess(0x1000,False,entry.th32ProcessID)
                if not process: raise RuntimeError("无法确认另一个软件窗口，未安装")
                try:
                    buffer,size = ctypes.create_unicode_buffer(32768),w.DWORD(32768)
                    if not dll.QueryFullProcessImageNameW(process,0,buffer,ctypes.byref(size)):
                        raise RuntimeError("无法检查软件目录，未安装")
                    if os.path.normcase(buffer.value)==expected: found.append(entry.th32ProcessID)
                finally: dll.CloseHandle(process)
            ok = dll.Process32NextW(snapshot,ctypes.byref(entry))
    finally: dll.CloseHandle(snapshot)
    return found


def _validate_https_url(value: object, field_name: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{field_name} 必须是 HTTPS 地址")
    return url


def _open_url(url: str, headers: dict[str, str]):
    """Open a URL with an environment-proxy attempt and a direct fallback.

    GitHub release downloads redirect to a separate asset host.  Some Windows
    networks can reach raw.githubusercontent.com but time out on that redirect
    host, while other networks need their configured proxy.  Trying both
    transports keeps the updater independent of the user's proxy setup.
    """

    return open_url(url,headers,timeout=DOWNLOAD_TIMEOUT_SECONDS)


def _read_manifest(url: str) -> dict[str, str]:
    manifest_url = _validate_https_url(url, "更新清单地址")
    with _open_url(
        manifest_url,
        {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "GupiaoStockTool-Updater/1.1",
        },
    ) as response:
        payload = json.loads(response.read(1024 * 1024 + 1).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("更新清单格式不正确")
    download_url = _validate_https_url(payload.get("url"), "下载地址")
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise ValueError("更新清单中的 SHA-256 不正确")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ValueError("更新清单缺少版本号")
    urls = payload.get("urls") or []
    if not isinstance(urls,list) or len(urls)>5:
        raise ValueError("备用下载地址不正确")
    return {"version": version, "url": download_url, "urls":[_validate_https_url(v,"备用下载地址") for v in urls], "sha256": sha256}


def _response_status(response: object) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    try:
        return int(status or 0)
    except (TypeError, ValueError):
        return 0


def _content_range_start(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Range", "") if headers is not None else ""
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(?:\d+|\*)", str(value).strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _hash_file(path: Path) -> hashlib._Hash:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest


def _download(url: str, expected_sha256: str, destination: Path, *, urls=None, progress=None, cancel=None) -> None:
    last_error: Exception | None = None
    sources = list(dict.fromkeys([*(urls or []),url]))
    if len(sources)>6: raise ValueError("下载地址过多")
    for source in sources: _validate_https_url(source,"下载地址")
    attempts = max(DOWNLOAD_ATTEMPTS,len(sources)*2)
    for attempt in range(1, attempts + 1):
        if cancel and cancel.is_set(): raise DownloadCancelled("已取消下载，原版本继续使用")
        resume_from = destination.stat().st_size if destination.exists() else 0
        digest = _hash_file(destination) if resume_from else hashlib.sha256()
        if resume_from and digest.hexdigest()==expected_sha256.lower():
            if progress: progress(resume_from,resume_from,attempt,len(sources))
            return
        try:
            headers = {
                "Accept": "application/zip,application/octet-stream",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "User-Agent": "GupiaoStockTool-Updater/1.2",
            }
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            with _open_url(
                sources[(attempt-1)%len(sources)],
                headers,
            ) as response:
                status = _response_status(response)
                range_start = _content_range_start(response)
                if status not in {200, 206}:
                    raise RuntimeError(f"下载服务器返回 HTTP {status}")
                if status == 206 and range_start != resume_from:
                    raise RuntimeError(
                        f"下载服务器返回了错误的分块起点：期望 {resume_from}，实际 {range_start}"
                    )
                append = status == 206 and resume_from > 0
                if not append:
                    resume_from = 0
                    digest = hashlib.sha256()
                with destination.open("ab" if append else "wb") as output:
                    bytes_written = resume_from
                    length = response.headers.get("Content-Length") if getattr(response,"headers",None) is not None else None
                    total = resume_from+int(length) if length and str(length).isdigit() else 0
                    if progress: progress(bytes_written,total,attempt,len(sources))
                    while True:
                        if cancel and cancel.is_set(): raise DownloadCancelled("已取消下载，原版本继续使用")
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
                        output.write(chunk)
                        bytes_written += len(chunk)
                        if progress: progress(bytes_written,total,attempt,len(sources))
                if bytes_written == 0:
                    raise RuntimeError("下载服务器返回空文件")
            actual = digest.hexdigest().lower()
            if actual != expected_sha256.lower():
                destination.unlink(missing_ok=True)
                raise ValueError(f"安装包校验失败：期望 {expected_sha256}，实际 {actual}")
            return
        except DownloadCancelled:
            raise
        except Exception as exc:
            last_error = exc
            # A complete/corrupt partial or rejected Range must restart, not
            # repeatedly request a range beyond the end of the same bad file.
            if getattr(exc,"code",None)==416 or "分块起点" in str(exc):
                destination.unlink(missing_ok=True)
            if attempt < attempts:
                delay = DOWNLOAD_RETRY_DELAYS[min(attempt-1,len(DOWNLOAD_RETRY_DELAYS)-1)]
                if cancel:
                    if cancel.wait(delay): raise DownloadCancelled("已取消下载")
                else:
                    time.sleep(delay)
    raise RuntimeError(
        f"下载更新包失败，已尝试 {attempts} 次及{len(sources)}个地址：{last_error}"
    ) from last_error


def _safe_extract(archive_path: Path, staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    base = staging_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            destination = (staging_dir / item.filename).resolve()
            if destination != base and base not in destination.parents:
                raise ValueError("安装包包含不安全的路径，已拒绝更新")
        archive.extractall(staging_dir)
    if (staging_dir / "GupiaoStockTool.exe").exists():
        return staging_dir
    directories = [item for item in staging_dir.iterdir() if item.is_dir()]
    if len(directories) == 1 and (directories[0] / "GupiaoStockTool.exe").exists():
        return directories[0]
    raise ValueError("安装包中没有找到 GupiaoStockTool.exe")


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _backup_user_data(target_dir: Path, backup_dir: Path) -> None:
    for name in (".env", "data", "reports"):
        source = target_dir / name
        if source.exists():
            _copy_tree(source, backup_dir / name)


def _backup_program_files(target_dir: Path, backup_dir: Path) -> None:
    program_backup = backup_dir / "program"
    for item in target_dir.iterdir():
        if item.name in PRESERVED_NAMES or item.name.startswith("update-"):
            continue
        if item.name == "GupiaoUpdater.exe":
            continue
        _copy_tree(item, program_backup / item.name)


def _install(staged_root: Path, target_dir: Path) -> None:
    for item in staged_root.iterdir():
        if item.name in PRESERVED_NAMES or item.name in SKIP_UPDATE_NAMES:
            continue
        _copy_tree(item, target_dir / item.name)


def _restore_program(target_dir: Path, backup_dir: Path) -> None:
    program_backup = backup_dir / "program"
    if not program_backup.exists():
        return
    for item in program_backup.iterdir():
        _copy_tree(item, target_dir / item.name)


def _restart(path: Path, target_dir: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"更新后找不到主程序：{path}")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(
        [str(path)],
        cwd=str(target_dir),
        close_fds=True,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def apply_update(manifest_url: str, target_dir: Path, parent_pid: int, restart_path: Path, *, prepared_archive=None, expected_sha256=None) -> None:
    target_dir = target_dir.resolve()
    temp_dir = Path(tempfile.mkdtemp(prefix="gupiao-update-"))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = target_dir / "update-backups" / timestamp
    try:
        archive_path = Path(prepared_archive) if prepared_archive else temp_dir / "update.zip"
        if prepared_archive:
            if not expected_sha256 or _hash_file(archive_path).hexdigest()!=expected_sha256.lower():
                raise ValueError("已下载的安装包校验失败，未替换程序")
        else:
            manifest = _read_manifest(manifest_url)
            _download(manifest["url"], manifest["sha256"], archive_path,urls=manifest["urls"])
        staging_dir = temp_dir / "staged"
        staged_root = _safe_extract(archive_path, staging_dir)
        _wait_for_parent(parent_pid)
        if other_install_processes(target_dir,parent_pid):
            raise RuntimeError("同一目录仍有其他软件窗口，请全部关闭后再更新")
        backup_dir.mkdir(parents=True, exist_ok=True)
        _backup_user_data(target_dir, backup_dir)
        _backup_program_files(target_dir, backup_dir)
        _install(staged_root, target_dir)
        _restart(restart_path.resolve(), target_dir)
    except Exception as exc:
        try:
            _restore_program(target_dir, backup_dir)
        except Exception as restore_exc:
            exc = RuntimeError(f"{exc}；回滚也失败：{restore_exc}")
        message = f"在线更新失败，已尝试保留原版本。\n\n{exc}\n\n详细信息：{_log_path(target_dir)}"
        _write_error(target_dir, message)
        _message(APP_NAME, message)
        try:
            _restart(restart_path.resolve(), target_dir)
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="GupiaoStockTool updater")
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--archive")
    parser.add_argument("--sha256")
    parser.add_argument("--target-dir", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--restart", required=True)
    args = parser.parse_args()
    apply_update(
        args.manifest_url,
        Path(args.target_dir),
        args.parent_pid,
        Path(args.restart),
        prepared_archive=args.archive,expected_sha256=args.sha256,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)
