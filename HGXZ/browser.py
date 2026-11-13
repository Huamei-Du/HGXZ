"""浏览器伪装的统一 HTTP 传输层。

原实现用 ``urllib.request`` 直接抓 HLS 分片，只带了两个头
（``User-Agent: Mozilla/5.0`` 和 ``Referer``）。在 CDN/WAF 眼里这三处都是死穴：

1. ``User-Agent: Mozilla/5.0`` 是 Python 爬虫的经典签名，真实浏览器永远不会这么短；
2. 缺 ``Accept`` / ``Accept-Language`` / ``sec-ch-ua`` / ``sec-fetch-*`` 一整套头，
   ``Accept-Encoding: identity`` 更是只有脚本才会发；
3. 更致命的是 TLS ClientHello（JA3/JA4）是 Python 自带 OpenSSL 的指纹，而且只跑
   HTTP/1.1 —— 光改请求头骗不过这一层。

做法是让 ``curl_cffi`` 负责“底层身份”（真实的 Chrome TLS 指纹 + HTTP/2），请求头
则由本模块按 Chrome 的真实顺序自己组装：``default_headers=False`` 关掉 libcurl 的
默认头，避免分片请求里莫名带上 ``Sec-Fetch-User: ?1`` / ``Upgrade-Insecure-Requests``
这类只有导航请求才有的头——那本身就是破绽。

另外整站共用一个会话：站点和 CDN 共享同一个 Cookie 罐、同一条连接池，就像同一个
浏览器标签页看过页面再去拉流。撞上 403/429 会丢弃会话重新预热，而不是硬顶着刷。

没装 curl_cffi 时退化成“头信息完备 + 带 Cookie 罐”的 urllib。
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.parse import urlsplit
from urllib.request import (HTTPCookieProcessor, Request, build_opener,
                            urlopen)
import gzip
import logging
import os
import random
import threading
import time
import zlib

LOG = logging.getLogger(__name__)

try:  # 真浏览器指纹（可选依赖，缺失时自动降级）
    from curl_cffi import requests as _curl_requests
except Exception:  # pragma: no cover - 仅在缺依赖时走到
    _curl_requests = None


# 资源类型 -> (Accept, sec-fetch-dest, sec-fetch-mode)
# 这些组合必须和真实浏览器发出去的一致，乱填比不填更可疑。
_RESOURCE = {
    'document': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,'
        'image/avif,image/webp,image/apng,*/*;q=0.8',
        'document', 'navigate'),
    'playlist': ('application/vnd.apple.mpegurl,application/x-mpegURL,*/*', 'empty', 'cors'),
    'video': ('*/*', 'video', 'no-cors'),
    'image': ('image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
              'image', 'no-cors'),
    'api': ('*/*', 'empty', 'cors'),
}

# Chrome 发子资源请求时的头部顺序（顺序也是指纹的一部分）
_HEADER_ORDER = (
    'User-Agent', 'Accept',
    'Sec-Ch-Ua', 'Sec-Ch-Ua-Mobile', 'Sec-Ch-Ua-Platform',
    'Upgrade-Insecure-Requests',
    'Sec-Fetch-Dest', 'Sec-Fetch-Mode', 'Sec-Fetch-Site',
    'Referer', 'Origin', 'Content-Type',
    'Accept-Encoding', 'Accept-Language', 'Priority', 'Connection',
)


@dataclass(frozen=True)
class BrowserProfile:
    """一套自洽的浏览器身份。UA 和 sec-ch-ua 必须版本一致，
    混搭（比如 UA 写 Chrome 131、sec-ch-ua 写 142）反而是强指纹。"""

    name: str
    impersonate: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    accept_language: str = 'zh-CN,zh;q=0.9,en;q=0.8'


def _chrome_profile(name: str, major: int, platform: str = 'Windows') -> BrowserProfile:
    if platform == 'Windows':
        ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              f'(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36')
    else:  # macOS
        ua = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              f'(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36')
    return BrowserProfile(
        name=name,
        impersonate=name,
        user_agent=ua,
        sec_ch_ua=(f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
                   '"Not_A Brand";v="99"'),
        sec_ch_ua_platform=f'"{platform}"',
    )


# 配置里用 site.browser.profile 指定。
PROFILES: dict[str, BrowserProfile] = {
    'chrome': _chrome_profile('chrome142', 142),
    'chrome142': _chrome_profile('chrome142', 142),
    'chrome136': _chrome_profile('chrome136', 136),
    'chrome131': _chrome_profile('chrome131', 131),
}


class BlockedError(RuntimeError):
    """被 WAF/CDN 拦下（403/429），值得换会话重试。"""


def _site_of(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def _short(url: str, limit: int = 110) -> str:
    return url if len(url) <= limit else url[:limit] + '...'


def _canonical(name: str) -> str:
    """把 'content-type' 之类统一成浏览器实际发的 'Content-Type'。"""
    return '-'.join(part[:1].upper() + part[1:].lower() for part in name.split('-'))


_PROXY_ENV_KEYS = ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                   'ALL_PROXY', 'all_proxy')


def _mask_proxy(url: str) -> str:
    """代理地址里的账号密码打码后再进日志。"""
    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ''
    if parts.port:
        host = f'{host}:{parts.port}'
    return f'{parts.scheme}://***@{host}'


def _env_proxy() -> str | None:
    """读环境变量里的出站代理。

    这里只用于“报告”，实际采用由 curl_cffi 的 trust_env 完成（它默认 True，
    libcurl 会自己读 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY）。两条路径必须对得上，
    否则日志里说有代理、实际却直连，排查时会走偏。
    """
    for key in _PROXY_ENV_KEYS:
        value = (os.environ.get(key) or '').strip()
        if value:
            return value
    return None


def _env_no_proxy() -> str:
    for key in ('NO_PROXY', 'no_proxy'):
        value = (os.environ.get(key) or '').strip()
        if value:
            return value
    return ''


def _fetch_site(referer: str | None, url: str) -> str:
    """sec-fetch-site: 同源 / 同站 / 跨站，浏览器就是这么算的。"""
    if not referer:
        return 'none'
    ref_host = urlsplit(referer).hostname or ''
    tgt_host = urlsplit(url).hostname or ''
    if ref_host == tgt_host:
        return 'same-origin'
    ref_base = '.'.join(ref_host.split('.')[-2:])
    tgt_base = '.'.join(tgt_host.split('.')[-2:])
    return 'same-site' if ref_base and ref_base == tgt_base else 'cross-site'


class BrowserSession:
    """一个长生命周期的“浏览器标签页”。"""

    def __init__(
        self,
        *,
        profile: str = 'chrome',
        timeout: float = 30.0,
        request_interval: float = 0.0,
        proxy: str | None = None,
    ):
        selected = PROFILES.get(profile) or PROFILES['chrome']
        available = _available_impersonations()
        if available and selected.impersonate not in available:
            # curl_cffi 版本较旧时退到泛化别名，指纹仍是最新的 Chrome。
            LOG.warning('指纹 %s 不可用，回退到 chrome', selected.impersonate)
            selected = PROFILES['chrome']
        self.profile = selected
        self.timeout = timeout
        self.request_interval = request_interval
        self.proxy = proxy
        self._lock = threading.RLock()
        self._backend = None
        self._backend_kind = ''
        self._warmed: set[str] = set()
        self._last_request = 0.0
        self._warned_fallback = False

    # ---------------------------------------------------------------- 会话管理

    @property
    def backend(self) -> str:
        self._ensure()
        return self._backend_kind

    def _ensure(self) -> None:
        if self._backend is not None:
            return
        with self._lock:
            if self._backend is not None:
                return
            if _curl_requests is not None:
                kwargs = {
                    'impersonate': self.profile.impersonate,
                    'timeout': self.timeout,
                    # 关掉 libcurl 自带的默认头，改由本模块完整组装，
                    # 这样分片请求不会带上导航请求才有的那些头。
                    'default_headers': False,
                }
                if self.proxy:
                    kwargs['proxy'] = self.proxy
                self._backend = _curl_requests.Session(**kwargs)
                self._backend_kind = f'curl_cffi/{self.profile.impersonate}'
                LOG.info('HTTP 传输层: %s（Chrome TLS 指纹 + HTTP/2）', self._backend_kind)
            else:
                self._backend = build_opener(HTTPCookieProcessor(CookieJar()))
                self._backend_kind = 'urllib-fallback'
                if not self._warned_fallback:
                    self._warned_fallback = True
                    LOG.warning(
                        '未安装 curl_cffi，退化为 urllib 传输层：请求头已补全、'
                        'Cookie 会保持，但 TLS 指纹仍是 Python 的，遇到强风控的 '
                        'CDN 仍可能被拦。建议 pip install "curl_cffi>=0.7"')
            self._log_route()

    def _log_route(self) -> None:
        """启动时把出站线路摊开写一行：配了代理却没生效，是最常见的“说不通”。"""
        if self.proxy:
            LOG.info('出站代理: %s（来自 config.json 的 site.browser.proxy）',
                     _mask_proxy(self.proxy))
            env_proxy = _env_proxy()
            if env_proxy and _mask_proxy(env_proxy) != _mask_proxy(self.proxy):
                LOG.info('  环境变量里的代理 %s 已被配置覆盖', _mask_proxy(env_proxy))
            return
        env_proxy = _env_proxy()
        if env_proxy:
            bypass = _env_no_proxy()
            LOG.info('出站代理: %s（来自环境变量%s）', _mask_proxy(env_proxy),
                     f'，直连 {bypass}' if bypass else '')
            return
        LOG.info('出站代理: 直连（config.json 与环境变量都没有设置）')

    def warmup(self) -> None:
        """启动阶段就把传输层建起来。

        好处是「指纹版本 / 出站代理」这类线路信息紧跟着版本横幅出现，不用等
        第一次同步；万一代理填错，也能在启动日志里第一时间看到走的是哪条路。
        """
        self._ensure()

    def reset(self) -> None:
        """丢掉整个会话（Cookie + 连接池），用于被拦后重新做人。"""
        with self._lock:
            backend = self._backend
            self._backend = None
            self._warmed.clear()
            if hasattr(backend, 'close'):
                try:
                    backend.close()
                except Exception:
                    pass

    def close(self) -> None:
        self.reset()

    def __enter__(self):
        self._ensure()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # ------------------------------------------------------------------ 请求头

    def _headers(
        self,
        url: str,
        referer: str | None,
        kind: str,
        extra: dict | None,
        method: str,
    ) -> dict[str, str]:
        self._ensure()
        accept, dest, mode = _RESOURCE.get(kind, _RESOURCE['api'])
        values = {
            'User-Agent': self.profile.user_agent,
            'Accept': accept,
            'Sec-Ch-Ua': self.profile.sec_ch_ua,
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': self.profile.sec_ch_ua_platform,
            'Sec-Fetch-Dest': dest,
            'Sec-Fetch-Mode': mode,
            'Sec-Fetch-Site': _fetch_site(referer, url),
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': self.profile.accept_language,
        }
        if dest == 'document':
            # 只有导航请求才带这两个头，分片/接口请求带上是明显的破绽。
            values['Upgrade-Insecure-Requests'] = '1'
            values['Priority'] = 'u=0, i'
        if referer:
            values['Referer'] = referer
        if method.upper() not in ('GET', 'HEAD'):
            # 浏览器的 fetch/XHR（含同源 POST）都会带 Origin。
            values['Origin'] = _site_of(referer) if referer else _site_of(url)
        if self._backend_kind == 'urllib-fallback':
            # urllib 走 HTTP/1.1，需要显式声明长连接，也解不了 br。
            values['Accept-Encoding'] = 'gzip, deflate'
            values['Connection'] = 'keep-alive'
        for key, value in (extra or {}).items():
            # 调用方显式指定的头优先，且要保留下来（之前会被顺序过滤掉）。
            for existing in [k for k in values if k.lower() == key.lower()]:
                del values[existing]
            values[_canonical(key)] = value
        ordered = {key: values[key] for key in _HEADER_ORDER if key in values}
        for key, value in values.items():
            if key not in ordered:
                ordered[key] = value
        return ordered

    # -------------------------------------------------------------------- 节流

    def _pace(self, enabled: bool) -> None:
        if not enabled or self.request_interval <= 0:
            return
        wait = self.request_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.2, self.request_interval / 2)))
        self._last_request = time.monotonic()

    # -------------------------------------------------------------------- 发送

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: float | None,
    ) -> bytes:
        self._ensure()
        timeout = self.timeout if timeout is None else timeout
        if _curl_requests is not None:
            response = self._backend.request(
                method, url, headers=headers, data=data,
                timeout=timeout, impersonate=self.profile.impersonate,
                allow_redirects=True)
            status = response.status_code
            if status in (403, 429):
                raise BlockedError(
                    f'HTTP {status} ({"forbidden" if status == 403 else "rate limited"})')
            if status >= 400:
                raise RuntimeError(f'HTTP {status} for {_short(url)}')
            return response.content

        request = Request(url, method=method, data=data, headers=headers)
        try:
            opener = self._backend if hasattr(self._backend, 'open') else urlopen
            with opener.open(request, timeout=timeout) as response:
                return self._decode(response.read(),
                                    response.headers.get('Content-Encoding', ''))
        except Exception as exc:
            status = getattr(exc, 'code', None)
            if status in (403, 429):
                raise BlockedError(f'HTTP {status}') from exc
            raise

    @staticmethod
    def _decode(raw: bytes, encoding: str) -> bytes:
        encoding = (encoding or '').lower().strip()
        try:
            if encoding == 'gzip':
                return gzip.decompress(raw)
            if encoding == 'deflate':
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            return raw
        return raw

    # -------------------------------------------------------------------- 预热

    def warm_up(self, url: str, *, force: bool = False) -> None:
        """打开站点首页把 Cookie 拿全（cf_clearance / session 之类），
        让后面的接口和分片请求看起来是“先逛页面再播视频”。"""
        origin = _site_of(url)
        if not origin:
            return
        with self._lock:
            if not force and origin in self._warmed:
                return
            self._warmed.add(origin)
        try:
            home = origin + '/'
            self._send('GET', home,
                       self._headers(home, home, 'document', None, 'GET'),
                       None, self.timeout)
            LOG.debug('已预热会话: %s', origin)
        except Exception as exc:
            LOG.debug('预热 %s 失败（继续）: %s', origin, exc)

    # -------------------------------------------------------------------- 接口

    def fetch(
        self,
        url: str,
        *,
        referer: str | None = None,
        kind: str = 'api',
        headers: dict | None = None,
        data: bytes | None = None,
        method: str | None = None,
        pace: bool = False,
        timeout: float | None = None,
        attempts: int = 2,
    ) -> bytes:
        method = method or ('POST' if data is not None else 'GET')
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._pace(pace)
            request_headers = self._headers(url, referer, kind, headers, method)
            try:
                return self._send(method, url, request_headers, data, timeout)
            except BlockedError as exc:
                last = exc
                if attempt >= attempts:
                    break
                LOG.warning('请求被拦（%s）：%s，重建浏览器会话后重试', exc, _short(url))
                self.reset()
                if referer:
                    self.warm_up(referer, force=True)
                    self._rewarm_page(referer)
                time.sleep(1.5 + random.uniform(0, 1.5))
        raise last if last else RuntimeError('request failed')

    def _rewarm_page(self, referer: str) -> None:
        """重建会话后重新打开一次播放页，把页面级 Cookie 再拿一遍。"""
        try:
            self._send('GET', referer,
                       self._headers(referer, referer, 'document', None, 'GET'),
                       None, self.timeout)
        except Exception as exc:
            LOG.debug('重建后打开页面失败（继续）: %s', exc)

    def text(self, url: str, **kwargs) -> str:
        return self.fetch(url, **kwargs).decode('utf-8', errors='replace')


def _available_impersonations() -> set[str]:
    if _curl_requests is None:
        return set()
    try:
        from curl_cffi.requests.impersonate import BrowserType
        return {item.value for item in BrowserType}
    except Exception:  # pragma: no cover
        return set()
