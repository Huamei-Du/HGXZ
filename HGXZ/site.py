from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
import json
import logging
import random
import time

from .browser import BrowserSession


LOG = logging.getLogger(__name__)


class HuangguoClient:
    def __init__(
        self,
        base_url: str,
        request_interval: float = 2.0,
        timeout: int = 30,
        backup_urls: list[str] | None = None,
        browser: BrowserSession | None = None,
        browser_profile: str = 'chrome',
        browser_proxy: str | None = None,
    ):
        urls = [base_url, *(backup_urls or [])]
        self.base_urls = list(dict.fromkeys(url.rstrip('/') for url in urls if url))
        if not self.base_urls:
            raise ValueError('at least one site base URL is required')
        self.base_url = self.base_urls[0]
        self.request_interval = request_interval
        self.timeout = timeout
        self.last_request = 0.0
        # 整站共用一个浏览器会话：Cookie 在站点接口和 CDN 分片之间延续，
        # 这才是真实浏览器“先看页面再播视频”的链路。
        self.session = browser or BrowserSession(
            profile=browser_profile,
            timeout=timeout,
            request_interval=min(request_interval, 0.4),
            proxy=browser_proxy,
        )
        self._backup_requests = 0

    @property
    def user_agent(self) -> str:
        return self.session.profile.user_agent

    def close(self) -> None:
        self.session.close()

    def _throttle(self) -> None:
        wait = self.request_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.request_interval / 4)))

    def _candidates(self, is_absolute: bool) -> list[str | None]:
        if is_absolute:
            return [None]
        candidates = [self.base_url, *[url for url in self.base_urls if url != self.base_url]]
        if self.base_url != self.base_urls[0]:
            # After a failover, probe the primary host now and then so a
            # transient outage does not pin us to a backup forever.
            self._backup_requests += 1
            if self._backup_requests >= 20:
                self._backup_requests = 0
                candidates = [self.base_urls[0], self.base_url, *[url for url in self.base_urls if url not in (self.base_urls[0], self.base_url)]]
        return candidates

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict | None = None, referer: str | None = None,
                 kind: str = 'api') -> bytes:
        self._throttle()
        is_absolute = path.startswith('http')
        candidates = self._candidates(is_absolute)
        last_error: Exception | None = None
        try:
            for index, candidate in enumerate(candidates):
                url = path if is_absolute else candidate + path
                if referer:
                    ref = referer
                elif is_absolute:
                    ref = self.base_url + '/'
                else:
                    ref = candidate + '/'
                # 新域名先像人一样打开一次首页，把 Cookie（cf_clearance 等）拿全。
                self.session.warm_up(ref if is_absolute else candidate)
                try:
                    body = self.session.fetch(
                        url, referer=ref, kind=kind, headers=headers,
                        data=data, method=method, timeout=self.timeout)
                    if candidate and candidate != self.base_url:
                        LOG.warning('site base URL switched to: %s', candidate)
                        self.base_url = candidate
                        self._backup_requests = 0
                    return body
                except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as error:
                    last_error = error
                    if is_absolute or index == len(candidates) - 1:
                        raise
                    LOG.warning('site request failed via %s; trying backup: %s', candidate, error)
            raise last_error or RuntimeError('site request failed')
        finally:
            self.last_request = time.monotonic()

    def get_text(self, path: str) -> str:
        return self._request('GET', path, kind='document').decode('utf-8', errors='replace')

    def get_bytes(self, url: str, referer: str | None = None,
                  kind: str = 'image') -> bytes:
        return self._request('GET', url, referer=referer, kind=kind)

    def page_referer(self, album_id: int) -> str:
        """播放页地址：分片请求的 Referer 用它才符合真实播放行为。"""
        return f'{self.base_url}/video/{int(album_id)}/'

    def play_url(self, album_id: int, episode: int) -> str:
        raw = self._request(
            'GET', f'/api/videos/{int(album_id)}/play?' + urlencode({'ep': int(episode)}),
            referer=self.page_referer(album_id), kind='api')
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'play endpoint failed')
        url = ((payload.get('data') or {}).get('video_url') or '').strip()
        if not url:
            raise RuntimeError('empty video_url')
        return url

    def cover_url(self, album_id: int) -> str:
        body = json.dumps({'items': [{'type': 'video', 'id': int(album_id)}]}, ensure_ascii=False).encode()
        raw = self._request('POST', '/api/media/covers', data=body,
                            headers={'Content-Type': 'application/json'},
                            referer=self.page_referer(album_id), kind='api')
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'cover endpoint failed')
        return (((payload.get('data') or {}).get('covers') or {}).get(f'video:{int(album_id)}') or '').strip()
