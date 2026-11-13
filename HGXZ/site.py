from __future__ import annotations

from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
import json
import logging
import random
import time

from . import edge
from .browser import BrowserSession


LOG = logging.getLogger(__name__)


def _status_of(error: Exception) -> int | None:
    """取出异常里携带的 HTTP 状态码（curl_cffi 走 status，urllib 走 code）。"""
    status = getattr(error, 'status', None) or getattr(error, 'code', None)
    return int(status) if isinstance(status, int) else None


def _missing_path(error: Exception) -> bool:
    """404/410 这类“站点说没这个地址”的错误。

    403/429 走 BlockedError（没有 status），仍然值得换域名换会话；而这几个
    状态码在每个备用域名上都会一模一样，换域名只是白白等掉十几个请求间隔。
    """
    status = _status_of(error)
    return status is not None and 400 <= status < 500


@dataclass
class PlaySource:
    """一次播放会话的清单地址与拉流所需请求头。"""

    url: str
    headers: dict = field(default_factory=dict)
    hevc: str = ''
    expires_at: int = 0
    source: str = 'edge'          # edge / legacy，便于日志区分走了哪条路

    def __bool__(self) -> bool:
        return bool(self.url)


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
                    if _missing_path(error):
                        # 地址本身不存在：备用域名会给出同样的 404，没必要逐个试。
                        raise RuntimeError(
                            f'{error}（备用域名同样会返回 {_status_of(error)}，'
                            '常见原因是 site.categories[].path 写错，例如多带了页码）'
                        ) from error
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
        return self.play(album_id, episode).url

    # ------------------------------------------------------------ 播放地址

    def _edge_channel(self) -> 'edge.EdgeChannel':
        """加密会话与 base_url 绑定，切域名后必须重开。"""
        channel = getattr(self, '_edge', None)
        if channel is None or channel.base_url != self.base_url:
            channel = edge.EdgeChannel(self.session, self.base_url, timeout=self.timeout)
            self._edge = channel
        return channel

    @staticmethod
    def _cdn_headers(cdn: dict | None) -> dict:
        """清单与分片要带 CDN 鉴权头，值由播放接口下发。"""
        cdn = cdn or {}
        headers = {}
        key, value = cdn.get('authKey'), cdn.get('authValue')
        if key and value:
            headers[str(key)] = str(value)
        if cdn.get('app'):
            headers['x-cdn-app'] = str(cdn['app'])
        return headers

    def _play_via_edge(self, album_id: int, episode: int) -> PlaySource | None:
        """加密通道：POST /api/videos/:id/episodes/:ep/playback。

        未加密的 GET /api/videos/:id/play 现在只回空壳（status=1 但字段全空），
        真正可用的地址只在这个接口里。
        """
        if not edge.available():
            if not getattr(self, '_edge_warned', False):
                self._edge_warned = True
                LOG.warning(
                    '缺少 cryptography，无法使用加密播放通道：'
                    '旧接口只会返回空壳地址。请确认镜像内已安装 cryptography>=42')
            return None
        status, _headers, payload = self._edge_channel().call(
            'playback', 'POST',
            f'/api/videos/{int(album_id)}/episodes/{int(episode)}/playback',
            referer=self.page_referer(album_id), body=b'{}',
            content_type='application/json')
        if status != 200:
            raise RuntimeError(f'playback endpoint HTTP {status}')
        body = json.loads(payload)
        if body.get('status') != 1:
            raise RuntimeError(body.get('msg') or 'playback endpoint failed')
        data = body.get('data') or {}
        streams = data.get('streams') or {}
        url = (streams.get('main') or '').strip()
        if not url:
            raise RuntimeError('empty video_url')
        return PlaySource(
            url=url,
            headers=self._cdn_headers(data.get('cdn')),
            hevc=(streams.get('hevc') or '').strip(),
            expires_at=int(data.get('timestamp') or 0),
            source='edge',
        )

    def _play_via_legacy(self, album_id: int, episode: int) -> PlaySource:
        """站点回滚到旧接口时的兜底路径。"""
        raw = self._request(
            'GET', f'/api/videos/{int(album_id)}/play?' + urlencode({'ep': int(episode)}),
            referer=self.page_referer(album_id), kind='api')
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'play endpoint failed')
        data = payload.get('data') or {}
        url = (data.get('video_url') or '').strip()
        if not url:
            raise RuntimeError('empty video_url')
        return PlaySource(url=url, headers={}, source='legacy')

    def play(self, album_id: int, episode: int) -> PlaySource:
        """取播放地址：优先加密通道，失败再退回旧接口。"""
        edge_error: Exception | None = None
        try:
            source = self._play_via_edge(album_id, episode)
        except Exception as exc:
            edge_error = exc
            LOG.debug('album=%s ep=%s 加密通道取播放地址失败: %s',
                      album_id, episode, exc)
        else:
            if source:            # 通道不可用时会返回 None，交给下面的回退
                self._note_channel(source)
                return source
            edge_error = '加密通道未启用'
        try:
            source = self._play_via_legacy(album_id, episode)
            LOG.debug('album=%s ep=%s 已回退旧接口', album_id, episode)
            self._note_channel(source)
            return source
        except Exception as exc:
            # 两边都失败时把原因都带上，否则只看到 "empty video_url" 没法定位
            raise RuntimeError(f'{exc}（加密通道: {edge_error}）') from None

    def _note_channel(self, source: PlaySource) -> None:
        """首次拿到地址时说明走的哪条通道，之后不再重复。"""
        if getattr(self, '_channel_noted', False):
            return
        self._channel_noted = True
        if source.source == 'edge':
            LOG.info('播放地址通道: edge 加密通道（POST /api/videos/:id/episodes/:ep/playback）')
        else:
            LOG.warning('播放地址通道: 旧明文接口 —— 说明加密通道没走通，'
                        '这条接口随时可能只回空壳')

    def cover_url(self, album_id: int) -> str:
        body = json.dumps({'items': [{'type': 'video', 'id': int(album_id)}]}, ensure_ascii=False).encode()
        raw = self._request('POST', '/api/media/covers', data=body,
                            headers={'Content-Type': 'application/json'},
                            referer=self.page_referer(album_id), kind='api')
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'cover endpoint failed')
        return (((payload.get('data') or {}).get('covers') or {}).get(f'video:{int(album_id)}') or '').strip()
