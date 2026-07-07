import os
import time
import queue
import threading
import yt_dlp
from typing import Dict, Generator
from urllib.parse import urlparse

# ─── Stall detection windows ────────────────────────────────────────────────────
_STALL_WARN_SECS  = 30   # warn user after 30s of no new bytes
_STALL_ABORT_SECS = 120  # abort after 120s total stall (truly stuck)

# ─── Platforms that use simpler format strings ──────────────────────────────────
_SIMPLE_FORMAT_PLATFORMS = {'instagram.com', 'facebook.com', 'fb.watch'}


def _is_simple_platform(url: str) -> bool:
    host = urlparse(url).hostname or ''
    return any(p in host for p in _SIMPLE_FORMAT_PLATFORMS)


def _fmt_bytes(n: float) -> str:
    if n <= 0:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_speed(bps: float) -> str:
    if not bps or bps <= 0:
        return ''
    return f"{_fmt_bytes(bps)}/s"


def _fmt_eta(secs) -> str:
    if secs is None or secs <= 0:
        return ''
    m, s = divmod(int(secs), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _friendly_error(raw: str) -> str:
    r = raw.lower()
    if 'private' in r:
        return "This video is private — it can't be downloaded."
    if 'unavailable' in r or 'not available' in r or 'removed' in r:
        return "This content is unavailable or has been removed."
    if 'age' in r or 'sign in' in r or 'login' in r or 'cookies' in r:
        return "This content requires sign-in or age verification."
    if 'copyright' in r or 'blocked' in r:
        return "This content is blocked due to a copyright claim."
    if '403' in r or 'forbidden' in r:
        return "Access denied (403 Forbidden) — the platform is blocking this download."
    if '404' in r or 'not found' in r:
        return "Content not found (404) — the link may be broken or deleted."
    if 'rate' in r or 'too many' in r:
        return "Rate limited by the platform — please wait a few minutes and try again."
    if 'format' in r and ('not available' in r or 'requested' in r):
        return "The requested quality/format is not available for this content."
    if 'stalled' in r or 'no progress' in r:
        return raw  # already friendly
    return f"Download failed: {raw[:200]}"


def process_item(item: Dict) -> Generator[Dict, None, None]:
    """
    Process a single queue item. Yields real-time status dicts:
      {"status": "Progress",   "percent": 0-100, "speed": "2.1 MB/s",
       "eta": "45s", "downloaded": "128 MB", "total": "320 MB", "message": "..."}
      {"status": "Merging",    "message": "Merging audio and video…"}
      {"status": "Warning",    "message": "..."}
      {"status": "Completed",  "file_path": "...", "message": "Done"}
      {"status": "Failed",     "error": "...", "message": "..."}

    Architecture: yt-dlp runs in a background daemon thread and pushes updates
    into a queue. The generator drains that queue, so Streamlit can update the
    UI in near real-time without blocking.
    """
    url     = item['url']
    mode    = item['mode']
    quality = item['quality']
    simple  = _is_simple_platform(url)

    download_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'downloads')
    os.makedirs(download_dir, exist_ok=True)

    yield {"status": "Progress", "percent": 0, "message": "Looking up content info…",
           "speed": "", "eta": "", "downloaded": "", "total": ""}

    # ── Build yt-dlp options ────────────────────────────────────────────────────
    ydl_opts = {
        'outtmpl':          os.path.join(download_dir, f"{item['id']}.%(ext)s"),
        'quiet':            True,
        'no_warnings':      True,
        'retries':          5,
        'fragment_retries': 5,
        'socket_timeout':   30,
        'noprogress':       False,
        # Merge video+audio into mp4 when possible
        'merge_output_format': 'mp4',
        # Bypass 403 errors by using alternative clients
        'extractor_args': {'youtube': ['player_client=ios,android,web']},
    }

    if mode == 'Audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
        }]
        if quality != 'Default':
            abr = quality.replace('kbps', '')
            ydl_opts['postprocessors'][0]['preferredquality'] = abr
            # Fallback chain for audio bitrate
            ydl_opts['format'] = f'bestaudio[abr<={abr}]/bestaudio/best'
    else:
        # Video
        if simple:
            # Instagram/Facebook don't expose format codes like YouTube
            # Just grab the best available
            ydl_opts['format'] = 'best'
        elif quality != 'Default':
            res = quality.replace('p', '')
            # Progressive fallback: exact height → below height → best
            ydl_opts['format'] = (
                f'bestvideo[height<={res}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={res}]+bestaudio/'
                f'best[height<={res}]/best'
            )
        else:
            ydl_opts['format'] = (
                'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
                'bestvideo+bestaudio/best'
            )

    # ── Queues for thread communication ─────────────────────────────────────────
    prog_q   = queue.Queue()   # progress updates from hook → generator
    result_q = queue.Queue()   # final result from thread → generator

    def _progress_hook(d):
        status = d.get('status', '')
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0) or 0
            total      = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            speed      = d.get('speed') or 0
            eta        = d.get('eta')
            percent    = int(downloaded / total * 100) if total > 0 else 0
            prog_q.put({
                "status":     "Progress",
                "percent":    min(percent, 95),   # reserve 95→100 for merge
                "speed":      _fmt_speed(speed),
                "eta":        _fmt_eta(eta),
                "downloaded": _fmt_bytes(downloaded),
                "total":      _fmt_bytes(total) if total > 0 else "?",
                "message":    f"Downloading {mode} ({quality})…",
            })
        elif status == 'finished':
            prog_q.put({
                "status": "Merging",
                "percent": 97,
                "message": "Merging audio and video tracks…",
                "speed": "", "eta": "", "downloaded": "", "total": "",
            })

    ydl_opts['progress_hooks'] = [_progress_hook]

    # ── Download thread ──────────────────────────────────────────────────────────
    def _download():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            result_q.put({"success": True})
        except yt_dlp.utils.DownloadError as e:
            result_q.put({"success": False, "error": _friendly_error(str(e))})
        except Exception as e:
            result_q.put({"success": False, "error": str(e)})

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()

    # ── Drain the progress queue while thread is alive ───────────────────────────
    last_bytes_time = time.time()
    last_percent    = 0
    warned_stall    = False

    while thread.is_alive() or not prog_q.empty():
        try:
            update = prog_q.get(timeout=0.5)
            pct = update.get("percent", last_percent)
            if pct > last_percent:
                last_percent    = pct
                last_bytes_time = time.time()
                warned_stall    = False
            yield update

        except queue.Empty:
            idle = time.time() - last_bytes_time

            if idle >= _STALL_ABORT_SECS and last_percent > 0:
                # Truly stuck mid-download — abort
                thread.join(timeout=1)
                yield {
                    "status":  "Failed",
                    "message": f"Download stalled for {int(idle)}s with no progress — the server may be throttling. Please retry.",
                    "error":   "stall_abort",
                }
                return

            elif idle >= _STALL_WARN_SECS and not warned_stall and last_percent > 0:
                warned_stall = True
                yield {
                    "status":  "Warning",
                    "percent": last_percent,
                    "message": f"No progress for {int(idle)}s — slow connection or server throttling. Still trying…",
                    "speed": "", "eta": "", "downloaded": "", "total": "",
                }

    thread.join()

    # ── Read final result ────────────────────────────────────────────────────────
    try:
        result = result_q.get_nowait()
    except queue.Empty:
        result = {"success": False, "error": "Download thread exited with no result (unexpected)."}

    if result["success"]:
        # Find the output file (yt-dlp may have renamed/merged it)
        candidates = sorted(
            [f for f in os.listdir(download_dir) if f.startswith(item['id'])],
            key=lambda f: os.path.getmtime(os.path.join(download_dir, f)),
            reverse=True
        )
        if candidates:
            file_path = os.path.join(download_dir, candidates[0])
            yield {
                "status":    "Completed",
                "percent":   100,
                "message":   "Download complete!",
                "file_path": file_path,
                "speed": "", "eta": "", "downloaded": _fmt_bytes(os.path.getsize(file_path)), "total": "",
            }
        else:
            yield {
                "status":  "Failed",
                "message": "Download reported success but the output file is missing from disk.",
                "error":   "file_not_found",
            }
    else:
        yield {
            "status":  "Failed",
            "message": result["error"],
            "error":   result["error"],
        }
