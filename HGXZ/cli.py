from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import logging
import logging.handlers
import os
import signal
import threading
import time
import re

from .core import (Episode, build_episode_filename, category_label_matches,
                   parse_category_panel, parse_detail_html, sanitize_filename,
                   write_episode_nfo, write_tvshow_nfo)
from .download import download_hls, probe_media, validate_media
from .cover import valid_cover, write_cover
from .site import HuangguoClient
from .state import StateDB
from .version import build_label

LOG = logging.getLogger('HGXZ')


def make_client(site: dict) -> HuangguoClient:
    """按配置装配站点客户端（含浏览器伪装参数）。"""
    browser_cfg = site.get('browser') or {}
    return HuangguoClient(
        site['base_url'],
        float(site.get('request_interval', 2)),
        int(site.get('timeout', 30)),
        backup_urls=site.get('backup_urls', []),
        browser_profile=str(browser_cfg.get('profile', 'chrome')),
        browser_proxy=(str(browser_cfg['proxy']) if browser_cfg.get('proxy') else None),
    )


class Archiver:
    MAX_CATEGORY_PAGES = 100

    def __init__(self, config: dict):
        self.config = config
        site = config['site']
        self.client = make_client(site)
        self.root = Path(config['download']['root'])
        self.state = StateDB(Path(config['state']['database']))
        self.retries = int(config['download'].get('retries', 3))
        self.minimum_duration = float(config['download'].get('minimum_duration', 3))
        self.recheck_days = float(config['download'].get('recheck_days', 7))
        self.failure_cooldown_hours = float(config['download'].get('failure_cooldown_hours', 24))
        self.episode_timeout = float(config['download'].get('episode_timeout', 1800))
        # 分类校验严格度：compat=按包含/缩写判定（默认），off=完全信任发现侧分类
        self.category_check = str(site.get('category_check') or 'compat').lower()
        # 详情页分类标签与配置不一致时只提示一次，避免每部剧刷一行日志
        self._category_notes: set[str] = set()

    def close(self):
        self.client.close()
        self.state.close()

    def discover(self) -> list[tuple[int, dict]]:
        seen: set[int] = set()
        out: list[tuple[int, dict]] = []
        for category in self.config['site']['categories']:
            page = 1
            category_ids: list[int] = []
            known_total: int | None = None
            while page <= self.MAX_CATEGORY_PAGES:
                path = category['path'] if page == 1 else category['path'].rstrip('/') + f'/{page}/'
                html = self.client.get_text(path)
                ids, page_total = parse_category_panel(html)
                if page_total is not None:
                    known_total = page_total
                fresh = [i for i in ids if i not in category_ids]
                category_ids.extend(fresh)
                if not fresh:
                    # Last page reached, or the site ignored our page number
                    # and re-served the same content; stop either way.
                    break
                if known_total is not None and len(category_ids) >= known_total:
                    break
                page += 1
            else:
                LOG.warning('category=%s hit the %d-page cap, results may be incomplete',
                            category['name'], self.MAX_CATEGORY_PAGES)
            LOG.info('category=%s discovered=%d', category['name'], len(category_ids))
            for album_id in category_ids:
                if album_id not in seen:
                    seen.add(album_id)
                    out.append((album_id, category))
        return out

    def _album_dir(self, album, category: dict) -> Path:
        library = sanitize_filename(category['library'])
        row = self.state.album_row(album.id)
        stored = (row['directory'] if row else '') or ''
        if stored:
            stored_path = Path(stored)
            # Prefer the directory recorded in the state DB so a title change
            # on the site does not strand the old folder (and duplicate the
            # album in Emby). Only trust paths that still exist inside the
            # current media root and library.
            try:
                if (stored_path.is_dir() and stored_path.is_relative_to(self.root)
                        and stored_path.parent.name == library):
                    return stored_path
            except OSError:
                pass
        return self.root / library / f'{sanitize_filename(album.title)} [huangguo-{album.id}]'

    def _complete_and_fresh(self, album_id: int) -> bool:
        row = self.state.album_row(album_id)
        if row is None or not row['ended'] or row['episode_count'] < 1:
            return False
        if not self.state.album_is_complete(album_id, row['episode_count']):
            return False
        if not row['last_checked_at']:
            return False
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row['last_checked_at'])
        except ValueError:
            return False
        return age.total_seconds() < self.recheck_days * 86400

    def _sync_cover(self, album, poster: Path) -> None:
        # Two independent sources: the API returns an AES-encrypted image,
        # the og:image fallback is usually plain. A failure in one must not
        # prevent trying the other.
        for source, url in (('api', None), ('og:image', album.cover_url)):
            try:
                if source == 'api':
                    url = self.client.cover_url(album.id)
                if not url:
                    continue
                write_cover(self.client.get_bytes(url, referer=self.client.page_referer(album.id)),
                            poster)
                return
            except Exception as exc:
                LOG.warning('album=%s cover via %s failed: %s',
                            album.id, source, exc)

    def _recorded_video_path(self, album_id: int, number: int) -> Path | None:
        """Path the episode was actually downloaded to, if it still exists."""
        row = self.state.episode_row(album_id, number)
        if not row or not row['path']:
            return None
        path = Path(row['path'])
        return path if path.exists() else None

    @staticmethod
    def _find_episode_video(season_dir: Path, number: int) -> Path | None:
        """Locate an already-downloaded episode regardless of its title prefix.

        The site occasionally renames a show; the existing files keep the old
        name, so matching on the episode number alone avoids downloading a
        second copy into the same folder.
        """
        pattern = re.compile(rf'\.S01E{number:02d}\.(mp4|mkv|ts)$', re.I)
        try:
            for candidate in season_dir.iterdir():
                if candidate.is_file() and pattern.search(candidate.name):
                    return candidate
        except OSError:
            return None
        return None

    def sync_album(self, album_id: int, category: dict, *,
                   metadata_only: bool = False,
                   max_episodes: int | None = None,
                   force: bool = False) -> dict:
        html = self.client.get_text(f'/video/{album_id}/')
        album = parse_detail_html(album_id, category['name'], html)
        cat_match = next((c for c in self.config['site']['categories'] if c['name'] == album.category), None)
        if cat_match:
            category = cat_match
        elif album.category != category['name']:
            if self.category_check == 'off' or category_label_matches(album.category, category['name']):
                # 详情页只写了短标签（「短剧」对「AI成人短剧」），不是分类冲突：
                # 以发现侧的分类为准归档，并把 album.category 归一成配置名，
                # 免得状态库和 WebUI 里多出一个没人管的分类。
                key = f"{category['name']}<-{album.category}"
                if key not in self._category_notes:
                    self._category_notes.add(key)
                    LOG.info('分类标签归一: 详情页写作 %r，按 %r 归档（同类不再提示）',
                             album.category, category['name'])
                album = replace(album, category=category['name'])
            else:
                LOG.warning('skip album=%s expected=%s actual=%s',
                            album.id, category['name'], album.category)
                return {'album_id': album.id, 'skipped': True,
                        'expected_category': category['name'],
                        'actual_category': album.category}
        album_dir = self._album_dir(album, category)
        self.state.upsert_album(album.id, album.category, album.title,
                                album.episode_count, album.ended,
                                str(album_dir))
        season_dir = album_dir / 'Season 01'
        season_dir.mkdir(parents=True, exist_ok=True)
        write_tvshow_nfo(album_dir / 'tvshow.nfo', album)
        poster = album_dir / 'poster.jpg'
        if not valid_cover(poster):
            self._sync_cover(album, poster)
        limit = album.episode_count if max_episodes is None else min(album.episode_count, max_episodes)
        done = 0
        failed_episodes: list[int] = []
        pending = [n for n in range(1, limit + 1) if not self.state.episode_done(album.id, n)]
        if pending and not metadata_only:
            LOG.info('album=%s [%s] 开始同步: 待下载 %d/%d 集', album.id, album.title, len(pending), limit)
        for number in range(1, limit + 1):
            video = season_dir / build_episode_filename(album.title, number)
            ep = Episode(album.id, number, f'S01E{number:02d}', album.plot)
            if metadata_only:
                write_episode_nfo(video.with_suffix('.nfo'), album, ep)
                continue
            if self.state.episode_done(album.id, number):
                # The file was downloaded under an older title, so its name no
                # longer matches the one derived from the current title; keep
                # the sidecar next to the video it actually describes.
                recorded = self._recorded_video_path(album.id, number) or video
                if not recorded.with_suffix('.nfo').exists():
                    write_episode_nfo(recorded.with_suffix('.nfo'), album, ep)
                done += 1
                continue
            # A rename on the site must not trigger a re-download of episodes
            # that are already sitting in this folder under the old name.
            existing = self._find_episode_video(season_dir, number)
            if existing is None:
                write_episode_nfo(video.with_suffix('.nfo'), album, ep)
                if video.exists():
                    existing = video
            if existing is not None:
                try:
                    info = probe_media(existing)
                    if validate_media(info, self.minimum_duration):
                        self.state.mark_episode(album.id, number, str(existing), 'done',
                                                info.get('size', 0))
                        if not existing.with_suffix('.nfo').exists():
                            write_episode_nfo(existing.with_suffix('.nfo'), album, ep)
                        done += 1
                        continue
                except Exception:
                    pass
            if not force:
                failed_at = self.state.episode_failed_at(album.id, number)
                if failed_at:
                    try:
                        age = datetime.now(timezone.utc) - datetime.fromisoformat(failed_at)
                    except ValueError:
                        age = None
                    if age is not None and age.total_seconds() < self.failure_cooldown_hours * 3600:
                        LOG.info('album=%s ep=%s in cooldown, skipping', album.id, number)
                        continue
            LOG.info('album=%s [%s] 第 %d/%d 集 开始下载...', album.id, album.title, number, limit)
            last = None
            succeeded = False
            for attempt in range(1, self.retries + 1):
                try:
                    source = self.client.play(album.id, number)
                    info = download_hls(source.url, video, self.client.page_referer(album.id),
                                        self.minimum_duration, timeout=self.episode_timeout,
                                        session=self.client.session, headers=source.headers)
                    self.state.mark_episode(album.id, number, str(video), 'done',
                                            info.get('size', 0))
                    done += 1
                    succeeded = True
                    size_mb = (info.get('size', 0) or 0) / (1024 * 1024)
                    LOG.info('album=%s [%s] 第 %d/%d 集 下载完成 (时长 %.0f秒, 大小 %.1fMB)',
                             album.id, album.title, number, limit, info.get('duration', 0), size_mb)
                    break
                except Exception as exc:
                    last = exc
                    err_msg = str(exc).lower()
                    # 网络层抖动（5xx / 超时 / 连接被重置）必须重试；
                    # 只有“站点明确说这集没了”才算永久失败。
                    transient = any(k in err_msg for k in (
                        'http 5', '502', '503', '504', 'timeout', 'timed out',
                        'connection', 'reset', 'eof', 'broken pipe'))
                    is_permanent = (not transient) and any(k in err_msg for k in (
                        'empty video_url', 'play endpoint failed', 'not found', '404', '403',
                        '不存在', '已下架', '下架', '加密通道未启用'
                    ))
                    LOG.warning('album=%s ep=%s attempt=%s failed=%s',
                                album.id, number, attempt, exc)
                    if is_permanent:
                        LOG.error('album=%s ep=%s permanent failure, skipping retries: %s',
                                  album.id, number, exc)
                        break
                    if attempt < self.retries:
                        time.sleep([5, 15, 30][min(attempt - 1, 2)])
            if not succeeded:
                # Recorded even for permanent failures so the cooldown applies,
                # the WebUI shows the episode as failed, and later syncs do not
                # keep hammering a withdrawn episode.
                self.state.mark_episode(album.id, number, str(video), 'failed')
                failed_episodes.append(number)
                LOG.error('album=%s ep=%s giving up after %s attempt(s): %s',
                          album.id, number, attempt, last)
        if pending and done == limit and not metadata_only:
            LOG.info('album=%s [%s] 全部 %d 集已下载完毕', album.id, album.title, limit)
        result = {'album': asdict(album), 'directory': str(album_dir),
                  'episodes_done': done, 'metadata_only': metadata_only}
        if failed_episodes:
            result['failed_episodes'] = failed_episodes
            LOG.error('album=%s finished with %d failed episodes: %s',
                      album.id, len(failed_episodes), failed_episodes)
        return result

    def scan(self, *, metadata_only=False, only_id=None,
             max_albums=None, max_episodes=None):
        force = bool(only_id)
        if only_id:
            found = [(only_id, self.config['site']['categories'][0])]
        else:
            found = self.discover()
        if max_albums:
            found = found[:max_albums]
        results = []
        failures = 0
        skipped_complete = 0
        for album_id, category in found:
            if not force and self._complete_and_fresh(album_id):
                skipped_complete += 1
                continue
            try:
                result = self.sync_album(
                    album_id, category, metadata_only=metadata_only,
                    max_episodes=max_episodes, force=force)
                results.append(result)
                if result.get('failed_episodes'):
                    failures += 1
            except Exception as exc:
                failures += 1
                LOG.exception('sync album %s failed: %s', album_id, exc)
        episodes_done = sum(r.get('episodes_done', 0) for r in results)
        LOG.info('sync finished synced=%d failures=%d skipped=%d episodes=%d',
                 len(results), failures, skipped_complete, episodes_done)
        return results, failures


class ConfigError(RuntimeError):
    """配置文件缺失或写错，用一句人话代替 traceback。"""


_REQUIRED_PATHS = (
    ('site', 'base_url'),
    ('download', 'root'),
    ('state', 'database'),
)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(
            f'配置文件不存在: {path}\n'
            '  Docker 部署要多做一步：把仓库里的 config.example.json 复制到宿主机上\n'
            '  挂载到 /config 的那个目录，并改名为 config.json，然后重启容器。\n'
            '  也请顺手确认 compose 的 volumes 里确实有 "<宿主目录>:/config"。')
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f'配置文件 JSON 语法错误: {path} 第 {exc.lineno} 行第 {exc.colno} 列 — {exc.msg}\n'
            '  JSON 不允许多余的逗号、注释和单引号，改完再重启。') from None
    if not isinstance(config, dict):
        raise ConfigError(f'配置文件顶层必须是一个 JSON 对象: {path}')
    for section, key in _REQUIRED_PATHS:
        block = config.get(section)
        if not isinstance(block, dict) or not block.get(key):
            raise ConfigError(
                f'配置文件缺少必填项 {section}.{key}: {path}\n'
                '  直接对照 config.example.json 补齐即可。')
    return config


def run_daemon(config: dict, schedule: str, web_port: int = 8099) -> int:
    from .web.app import (create_app, mark_sync_finished, release_sync,
                          set_shared_state, try_acquire_sync)
    from .web.log_handler import LogBuffer
    import uvicorn

    log_buf = LogBuffer()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    log_buf.setFormatter(formatter)
    root_logger = logging.getLogger('HGXZ')
    root_logger.addHandler(log_buf)

    log_dir_str = config.get('log', {}).get('directory', '')
    if log_dir_str:
        log_dir = Path(log_dir_str)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / 'hgxz.log', when='midnight', backupCount=30,
            encoding='utf-8')
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # 必须放在 handler 装好之后：这样横幅既能进日志文件，也能进 WebUI 的日志面板，
    # 成为启动后的第一行，方便确认容器跑的是哪一次构建。
    LOG.info('HGXZ %s', build_label())

    archiver = Archiver(config)
    # 让「指纹版本 / 出站代理」紧跟版本横幅出现在启动日志里，别等第一次同步
    try:
        archiver.client.session.warmup()
    except Exception as exc:  # 传输层起不来也要把原因说清楚，而不是静默
        LOG.warning('HTTP 传输层初始化失败: %s', exc)
    archiver_ref = [archiver]
    set_shared_state(archiver.state, log_buf, archiver_ref)

    from .web.app import _sync_status

    try:
        hour, minute = (int(part) for part in schedule.split(':'))
    except ValueError:
        LOG.error('invalid --schedule %r, expected HH:MM', schedule)
        return 1

    def sync_loop():
        while True:
            # Manual syncs (WebUI) and this loop must never run concurrently:
            # they would download the same episode into the same .part file.
            if not try_acquire_sync():
                LOG.warning('scheduled sync skipped: another sync is already running')
            else:
                try:
                    archiver.scan()
                    mark_sync_finished()
                except Exception as exc:
                    _sync_status['last_error'] = str(exc)
                    LOG.exception('sync failed')
                finally:
                    release_sync()

            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            LOG.info('next sync at %s (%.0f seconds from now)',
                     target.strftime('%Y-%m-%d %H:%M:%S'), wait_seconds)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                time.sleep(30)

    sync_thread = threading.Thread(target=sync_loop, daemon=True, name='sync-daemon')
    sync_thread.start()

    def _handle_stop(signum, _frame):
        # docker stop / 面板上的“停止”先发 SIGTERM，10 秒内没退出就升级成 SIGKILL，
        # 容器层面显示为 137（128+9）。同步线程可能正卡在 libcurl 的 C 调用或
        # ffmpeg 子进程上，等它自然收尾往往超过 10 秒，所以这里记一行日志就退出：
        # 停止动作干脆，日志里也能看出是“被人叫停”而不是“崩了”。
        # 未下完的分片只是 .part，下次同步会重下这一段，不会污染成片。
        name = signal.Signals(signum).name
        LOG.info('收到 %s，退出（未完成的下载会在下次同步时续上）', name)
        logging.shutdown()
        os._exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _handle_stop)
        except (ValueError, OSError):
            # 非主线程或环境不支持时忽略，不影响正常跑
            pass
    if hasattr(signal, 'SIGBREAK'):
        # Windows 上 Ctrl+Break / 停止进程走这条，本地调试时也能干净退出
        try:
            signal.signal(signal.SIGBREAK, _handle_stop)
        except (ValueError, OSError):
            pass

    LOG.info('WebUI starting on port %s', web_port)
    app = create_app()
    uvicorn.run(app, host='0.0.0.0', port=web_port, log_level='warning')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog='HGXZ', description='黄果下载')
    parser.add_argument('--config', default='/config/config.json')
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--only-id', type=int)
    parser.add_argument('--max-albums', type=int)
    parser.add_argument('--max-episodes', type=int)
    parser.add_argument('--rebuild-covers', action='store_true')
    parser.add_argument('--scan-existing', action='store_true',
                        help='扫描媒体目录，将已有视频注册到状态库')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--daemon', action='store_true',
                        default=os.environ.get('DAEMON', '').lower() in ('1', 'true', 'yes'),
                        help='常驻运行：启动后同步一次，之后每天定时同步，同时开启 WebUI')
    parser.add_argument('--schedule', default=os.environ.get('SCHEDULE', '03:30'),
                        help='--daemon 模式下的每日同步时间 HH:MM')
    parser.add_argument('--port', type=int, default=8099,
                        help='WebUI 端口（默认 8099）')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if not args.daemon:
        # --daemon 由 run_daemon 在装好 handler 之后再打横幅，这里不重复打。
        logging.getLogger('HGXZ').info('HGXZ %s', build_label())
    umask_val = os.environ.get('UMASK')
    if umask_val:
        try:
            os.umask(int(umask_val, 8))
        except Exception as exc:
            logging.getLogger('HGXZ').warning('failed to set umask=%s: %s', umask_val, exc)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        LOG.error('%s', exc)
        return 2

    if args.daemon:
        return run_daemon(config, args.schedule, args.port)

    if args.scan_existing:
        from .scanner import scan_existing
        state = StateDB(Path(config['state']['database']))
        try:
            result = scan_existing(
                Path(config['download']['root']), state,
                float(config['download'].get('minimum_duration', 3)))
        finally:
            state.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.rebuild_covers:
        from .rebuild_covers import rebuild
        client = make_client(config['site'])
        result = rebuild(Path(config['download']['root']), client)
        client.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['failed'] == 0 else 1

    archiver = Archiver(config)
    try:
        results, failures = archiver.scan(
            metadata_only=args.metadata_only, only_id=args.only_id,
            max_albums=args.max_albums, max_episodes=args.max_episodes)
    finally:
        archiver.close()
    if args.json:
        print(json.dumps({'results': results, 'failures': failures},
                         ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
