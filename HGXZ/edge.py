"""站点 edge 加密通道（protocol = ``dx150-edge-v1``）。

站点在 2026-11 前后把播放相关接口收进了加密通道：``GET /api/videos/:id/play``
这类路由直接请求会返回 **status=1 但所有字段为空** 的空壳——看起来成功，其实
服务端没给任何地址。真正可用的地址来自加密通道里的
``POST /api/videos/:id/episodes/:ep/playback``。

协议（照 ``/static/web/js/edge-api-crypto.js`` 复刻）：

1. 握手 ``POST /api/<scope>/request``
   ``{"v":1,"type":"hello","client_pub":"<b64url(ECDH P-256 未压缩公钥)>"}``
   返回 ``{"v":1,"type":"hello","kid","sid","site","server_pub","salt","server_time","expires_at"}``

2. 派生密钥
   ``shared = ECDH(client_priv, server_pub)``
   ``info   = "dx150-edge-v1\\n{scope}\\n{site}\\n{kid}\\n{sid}"``
   ``reqKey = HKDF-SHA256(salt, info + "\\nreq")`` / ``resKey = ... "\\nres"``

3. 调用 ``POST /api/<scope>/request``
   ``{"v":1,"type":"call","kid","sid","seq","ts","ciphertext":"<b64url>"}``
   IV = 12 字节：前 8 字节 0，后 4 字节 big-endian seq
   AAD = ``"{info}\\nreq\\n{seq}\\n{ts}"``
   明文 = ``{"request":{"method","path","body","content_type"}}``

4. 响应解密得 ``{"status":200,"headers":{...},"body":"<标准 base64>"}``

缺少 ``cryptography`` 依赖时整个通道不可用，调用方应回退到明文接口。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from urllib.parse import urlsplit

from .browser import BrowserSession

LOG = logging.getLogger(__name__)

try:  # 可选依赖：缺失时本模块整体停用，不影响其它功能
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    _CRYPTO_OK = True
except Exception:  # pragma: no cover - 仅在缺依赖时走到
    _CRYPTO_OK = False


PROTOCOL = 'dx150-edge-v1'
MAX_SEQUENCE = 16384

# scope -> 通道端点（与前端 endpoints 一致）
ENDPOINTS = {
    'user': '/api/user/request',
    'playback': '/api/playback/request',
    'public': '/api/public/request',
    'dynamic': '/api/dynamic/request',
}


def available() -> bool:
    return _CRYPTO_OK


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _b64url_dec(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _iv_for(sequence: int) -> bytes:
    return b'\x00' * 8 + int(sequence).to_bytes(4, 'big')


class EdgeError(RuntimeError):
    """加密通道不可用或响应异常。"""


class EdgeChannel:
    """一个 scope 上复用的加密会话。"""

    def __init__(self, browser: BrowserSession, base_url: str, *,
                 timeout: float = 30.0):
        self.browser = browser
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._states: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 握手

    def _post(self, path: str, payload: dict, referer: str) -> bytes:
        body = json.dumps(payload, separators=(',', ':')).encode()
        last: Exception | None = None
        # CDN 边缘节点偶发 502/超时，这类错误重试一次基本就好；
        # 4xx 属于协议/权限问题，重试只会白等。
        for attempt in range(1, 4):
            try:
                return self.browser.fetch(
                    self.base_url + path,
                    method='POST', data=body,
                    headers={'Content-Type': 'application/json'},
                    referer=referer, kind='api', timeout=self.timeout)
            except Exception as exc:
                last = exc
                text = str(exc)
                retryable = any(tag in text for tag in (
                    'HTTP 502', 'HTTP 503', 'HTTP 504', 'timeout', 'Timeout',
                    'timed out', 'reset', 'Connection', 'SSL', 'EOF'))
                if not retryable or attempt >= 3:
                    raise
                LOG.debug('edge 通道请求失败（第 %d 次），稍后重试: %s', attempt, exc)
                time.sleep(attempt * 1.5)
        raise last or EdgeError('edge 通道请求失败')

    def _open(self, scope: str, referer: str) -> dict:
        if not _CRYPTO_OK:
            raise EdgeError('缺少 cryptography 依赖，无法使用加密接口')

        private = ec.generate_private_key(ec.SECP256R1())
        client_pub = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

        raw = self._post(ENDPOINTS[scope],
                         {'v': 1, 'type': 'hello', 'client_pub': _b64url(client_pub)},
                         referer)
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError:
            raise EdgeError(f'握手响应不是 JSON: {raw[:160]!r}') from None

        if (hello.get('v') != 1 or hello.get('type') != 'hello'
                or not hello.get('kid') or not hello.get('sid')
                or not hello.get('server_pub') or not hello.get('salt')):
            raise EdgeError(f'握手响应字段不全: {raw[:200]!r}')

        server_pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _b64url_dec(hello['server_pub']))
        shared = private.exchange(ec.ECDH(), server_pub)

        salt = _b64url_dec(hello['salt'])
        if len(salt) != 32:
            raise EdgeError('握手 salt 长度异常')

        info = '\n'.join([PROTOCOL, scope, str(hello.get('site') or
                                              urlsplit(self.base_url).hostname),
                          hello['kid'], hello['sid']])

        def derive(direction: str) -> bytes:
            return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                        info=f'{info}\n{direction}'.encode()).derive(shared)

        local = int(time.time())
        state = {
            'info': info, 'kid': hello['kid'], 'sid': hello['sid'],
            'offset': int(hello['server_time']) - local,
            'expires_at': local + int(hello['expires_at']) - int(hello['server_time']),
            'sequence': 0,
            'req_key': derive('req'), 'res_key': derive('res'),
        }
        LOG.debug('edge 握手完成 scope=%s kid=%s', scope, state['kid'])
        return state

    def _state(self, scope: str, referer: str) -> dict:
        with self._lock:
            state = self._states.get(scope)
            if (state and state['expires_at'] > int(time.time()) + 5
                    and state['sequence'] < MAX_SEQUENCE):
                return state
            state = self._open(scope, referer)
            self._states[scope] = state
            return state

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    # ------------------------------------------------------------------ 调用

    def call(self, scope: str, method: str, path: str, *, referer: str,
             body: bytes = b'', content_type: str = '') -> tuple[int, dict, bytes]:
        """走加密通道发一次站内请求，返回 (状态码, 响应头, 响应体)。"""
        if scope not in ENDPOINTS:
            raise EdgeError(f'未知 scope: {scope}')
        with self._lock:
            state = self._state(scope, referer)
            state['sequence'] += 1
            sequence = state['sequence']

        timestamp = int(time.time()) + state['offset']
        request = {
            'method': method,
            'path': path,
            'body': base64.b64encode(body).decode() if body else '',
            'content_type': content_type,
        }
        plaintext = json.dumps({'request': request}, separators=(',', ':')).encode()
        aad = f'{state["info"]}\nreq\n{sequence}\n{timestamp}'.encode()
        ciphertext = AESGCM(state['req_key']).encrypt(
            _iv_for(sequence), plaintext, aad)

        raw = self._post(ENDPOINTS[scope], {
            'v': 1, 'type': 'call', 'kid': state['kid'], 'sid': state['sid'],
            'seq': sequence, 'ts': timestamp, 'ciphertext': _b64url(ciphertext),
        }, referer)

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise EdgeError(f'调用响应不是 JSON: {raw[:160]!r}') from None

        if result.get('type') != 'result':
            # session_expired 之类的错误会让本次会话作废，下次重新握手
            if str(result.get('error') or '') == 'session_expired':
                self.reset()
            raise EdgeError(f'调用失败: {raw[:200]!r}')
        if result.get('seq') != sequence:
            self.reset()
            raise EdgeError(f'seq 不匹配: 发 {sequence} 收 {result.get("seq")}')

        res_aad = f'{state["info"]}\nres\n{sequence}\n{timestamp}'.encode()
        try:
            inner = json.loads(AESGCM(state['res_key']).decrypt(
                _iv_for(sequence), _b64url_dec(result['ciphertext']), res_aad))
        except Exception as exc:
            self.reset()
            raise EdgeError(f'响应解密失败: {exc}') from None

        if not isinstance(inner, dict) or 'status' not in inner:
            raise EdgeError('响应结构异常')

        return (int(inner.get('status') or 0),
                inner.get('headers') or {},
                base64.b64decode(inner.get('body') or b''))
