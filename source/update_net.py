"""Standard-library-only HTTPS transport shared by app and standalone updater."""
import os
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, getproxies


def proxy_candidates():
    env = getproxies()
    selected = {k:v for k,v in env.items() if k in {"http","https","all"} and v}
    candidates = [selected] if selected else []
    if os.name == "nt":
        # Ignore unrelated settings such as ARK_USE_ENV_PROXY, which urllib
        # otherwise treats as a proxy and may mask Windows' actual proxy.
        try:
            from urllib.request import getproxies_registry
            system = {k:v for k,v in getproxies_registry().items() if k in {"http","https","all"} and v}
            if system and system not in candidates:
                candidates.append(system)
        except (ImportError,OSError):
            pass
    if {} not in candidates: candidates.append({})
    return candidates


def open_url(url,headers,timeout=12,deadline=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("更新网络地址必须是无凭据的HTTPS地址")
    errors = []
    for proxies in proxy_candidates():
        remaining = timeout if deadline is None else min(timeout,deadline-time.monotonic())
        if remaining<=0: break
        try:
            return build_opener(ProxyHandler(proxies)).open(Request(url,headers=headers),timeout=remaining)
        except Exception as exc:
            errors.append(type(exc).__name__)
    # Do not log URLs containing credentials, or private proxy addresses.
    raise RuntimeError("连接更新服务失败（代理/直连已尝试）："+"、".join(errors or ["超时"]))
