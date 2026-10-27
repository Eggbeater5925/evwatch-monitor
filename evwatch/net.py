"""
net.py — 带出口探测的 urlopen（默认直连，代理只作兜底）

结论（实测）：哈啰、铁塔都是国内域名，**直连最省事**。反而是代理会捣乱：
  * 系统代理（9091 之类）会把证书链换成它自己的 CA，Python/OpenSSL 验不过
  * 一旦验不过就会降级到「不校验 TLS」，比直连更糟
  * Clash 重启后端口会变，之前缓存的出口会一直超时（表现为 Errno 60）

策略：
  1. 设了 `EVWATCH_PROXY` 就只用它（明确指定优先）；
  2. 否则**先直连**；
  3. 直连真的不通，才依次试 7890/1087/6152/9091/9090 和 shell 里的 HTTPS_PROXY；
  4. 全部失败且没设 `EVWATCH_TLS_STRICT=1` 时，最后一次不校验 TLS（日志会醒目警告）；
  5. 缓存出口失效（超时/拒绝/证书）会自动丢弃并重新探测 —— 服务不会永久卡死。

launchd 起的后台服务不继承 shell 代理，正好就是「直连」这条路。
"""
from __future__ import annotations

import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request

CACHE: urllib.request.OpenerDirector | None = None
TRIED: list[str] = []
INSECURE = False
_SILENT = False


def _bootstrap_ca_env():
    """launchd 启动的进程环境里没有 CA 相关变量，ssl 默认路径可能不可用。
    这里显式把 SSL_CERT_FILE 指到 certifi 的包（只影响本进程）。"""
    if os.environ.get('SSL_CERT_FILE'):
        return
    ca = _cafile()
    if ca and os.path.isfile(ca):
        os.environ['SSL_CERT_FILE'] = ca
        os.environ.setdefault('SSL_CERT_DIR', os.path.dirname(ca))


def _cafile() -> str | None:
    """优先用 certifi 的包（比系统 openssl 默认路径更完整）"""
    try:
        import certifi
        return certifi.where()
    except Exception:
        return None


def _context(verify: bool = True) -> ssl.SSLContext:
    if not verify:
        ctx = ssl._create_unverified_context()
        return ctx
    ctx = ssl.create_default_context()
    ca = _cafile()
    if ca:
        try:
            ctx.load_verify_locations(ca)
        except Exception:
            pass
    return ctx


def _system_proxy() -> str | None:
    try:
        out = subprocess.run(['scutil', '--proxy'], capture_output=True, text=True, timeout=3).stdout
        host = re.search(r'HTTPSProxy\s*:\s*(\S+)', out) or re.search(r'HTTPProxy\s*:\s*(\S+)', out)
        port = re.search(r'HTTPSPort\s*:\s*(\d+)', out) or re.search(r'HTTPPort\s*:\s*(\d+)', out)
        if host and port:
            return f'http://{host.group(1)}:{port.group(1)}'
    except Exception:
        pass
    return None


def candidates() -> list[str | None]:
    """出口候选顺序：直连优先。

    这两个服务（哈啰、铁塔）都是国内域名，直连又快又不会有证书问题。
    代理只在「直连真的不通」时兜底，而且可能把证书链换成自己的 CA ——
    之前 9091 就这样让 Python 验不过，最后降级成不校验 TLS，反而更糟。
    想强制走代理：设 EVWATCH_PROXY=http://127.0.0.1:7890。
    """
    order: list[str | None] = []
    forced = os.environ.get('EVWATCH_PROXY')
    if forced:
        order.append(forced)                            # 显式指定就听它的
    order.append(None)                                  # 直连
    order += ['http://127.0.0.1:7890', 'http://127.0.0.1:1087',
              'http://127.0.0.1:6152', 'http://127.0.0.1:9091',
              'http://127.0.0.1:9090']                  # 直连不通才试代理
    for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        v = os.environ.get(k)
        if v:
            order.append(v)
    seen, uniq = set(), []
    for c in order:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _opener(proxy: str | None, verify: bool = True) -> urllib.request.OpenerDirector:
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy \
        else urllib.request.ProxyHandler({})
    https = urllib.request.HTTPSHandler(context=_context(verify))
    return urllib.request.build_opener(handler, https)


def _retryable(e: Exception) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return False                                    # 服务端已应答，别重放
    text = str(e)
    return any(k in text for k in ('CERTIFICATE_VERIFY_FAILED', 'Connection refused',
                                   'Connection reset', 'timed out', 'Name or service not known',
                                   'Tunnel connection failed', 'EOF occurred', 'Network is down'))


def _probe(req, timeout: float):
    """按候选顺序找一个能用的出口；成功返回 (resp, opener, proxy)"""
    global CACHE, INSECURE, _SILENT
    last: Exception | None = None
    for proxy in candidates():
        try:
            opener = _opener(proxy, verify=True)
            resp = opener.open(req, timeout=timeout)
            CACHE = opener
            TRIED.append(f'{proxy or "直连"} ✔')
            if not _SILENT:
                _SILENT = True
                print(f'   [net] 出口 {proxy or "直连"}，TLS 校验通过')
            return resp, opener, proxy
        except Exception as e:                          # noqa: BLE001
            last = e
            TRIED.append(f'{proxy or "直连"} ✘{type(e).__name__}:{str(e)[:40]}')
            if not _retryable(e):
                raise

    # 最后兜底：不校验 TLS（仅当明确允许）
    if os.environ.get('EVWATCH_TLS_STRICT') != '1':
        for proxy in candidates()[:2]:                  # 只试前两个，别在死端口上耗
            try:
                opener = _opener(proxy, verify=False)
                resp = opener.open(req, timeout=timeout)
                CACHE = opener
                if not INSECURE:
                    INSECURE = True
                    print('   [net] ⚠️ 所有出口的证书都验不过，已降级为不校验 TLS'
                          '（设 EVWATCH_TLS_STRICT=1 可禁止）')
                    print(f'   [net] 尝试详情：{describe()}')
                TRIED.append(f'不校验TLS({proxy or "直连"}) ✔')
                return resp, opener, proxy
            except Exception as e:                      # noqa: BLE001
                last = e
                TRIED.append(f'不校验TLS({proxy or "直连"}) ✘')
    raise last if last else RuntimeError('没有可用出口')


def urlopen(req, timeout: float = 15, stale_retry: bool = True):
    """带出口探测的 urlopen。

    注意：CACHE 里的 opener 指向的是**当时**那个代理端口。电脑上的 Clash 一重启端口就可能变，
    此时旧 opener 会一直超时（日志里表现为「尝试过」那串不变、每次都 Errno 60）——
    所以缓存失败时要丢掉缓存重新探一次，否则后台服务会永久卡死。
    """
    global CACHE
    if CACHE is not None:
        try:
            return CACHE.open(req, timeout=timeout)
        except Exception as e:                          # noqa: BLE001
            if not stale_retry or not _retryable(e):
                raise
            TRIED.append('缓存出口失效，重新探测')
            CACHE = None
            print(f'   [net] 缓存出口失效（{type(e).__name__}:{str(e)[:40]}），重新探测…')
    resp, _, _ = _probe(req, timeout)
    return resp


def describe() -> str:
    return ' → '.join(TRIED) if TRIED else '尚未发起请求'


_bootstrap_ca_env()
