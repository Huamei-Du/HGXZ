"""版本与构建信息。

Docker 构建时通过 build-arg 把 commit 与构建时间注入镜像（见 Dockerfile 与
.github/workflows/docker-build.yml），运行期从环境变量读回来。这样日志第一行
就能直接看出容器里跑的是哪一次构建——排查「明明重新构建了却还是旧行为」这类
问题时，不用再去猜到底是镜像没换、还是代码没生效。
"""
from __future__ import annotations

import os

__version__ = '1.1.1'

_RAW_COMMIT = (os.environ.get('HGXZ_BUILD_COMMIT') or '').strip()
_RAW_TIME = (os.environ.get('HGXZ_BUILD_TIME') or '').strip()


def _short_commit(value: str) -> str:
    if not value:
        return 'dev'
    # 源码目录里直接跑时没有注入，用 git 顶一下，实在没有就标 dev。
    return value[:7] if len(value) > 7 else value


BUILD_COMMIT = _short_commit(_RAW_COMMIT)
BUILD_TIME = _RAW_TIME or 'unknown'


def build_label() -> str:
    """形如 ``1.1.1 (build 4bd6d95 @ 2026-11-13T02:12:00Z)``。"""
    return f'{__version__} (build {BUILD_COMMIT} @ {BUILD_TIME})'
