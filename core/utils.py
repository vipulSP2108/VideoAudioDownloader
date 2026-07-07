import re
import yt_dlp
from typing import Tuple, List
from urllib.parse import urlparse

# ─── Strict Allowed Domain Map ──────────────────────────────────────────────────
_ALLOWED_DOMAINS = {
    'youtube.com':   ['www', 'm', 'music', 'web', ''],
    'youtu.be':      ['', 'www'],
    'instagram.com': ['www', 'm', 'web', ''],
    'facebook.com':  ['www', 'm', 'web', 'l', ''],
    'fb.watch':      ['', 'www'],
}


def is_valid_url(url: str) -> bool:
    """
    Strict URL validation.
    - Must start with http:// or https://
    - Must be from an explicitly allowed domain with an allowed subdomain
    - Must have a non-empty path (not just the root of the domain)
    """
    try:
        if not url.startswith(('http://', 'https://')):
            return False
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        path = parsed.path or ''

        for domain, allowed_subs in _ALLOWED_DOMAINS.items():
            if hostname == domain or hostname == f'www.{domain}':
                # exact domain match (e.g. youtu.be, fb.watch)
                if path and path != '/':
                    return True
            elif hostname.endswith(f'.{domain}'):
                # subdomain match — check it's in allowed list
                sub = hostname[: -(len(domain) + 1)]
                if sub in allowed_subs and path and path != '/':
                    return True
        return False
    except Exception:
        return False


def get_platform(url: str) -> str:
    """Return a human-readable platform name for a validated URL."""
    hostname = urlparse(url).hostname or ''
    if 'youtube' in hostname or 'youtu.be' in hostname:
        return 'YouTube Music' if 'music.' in hostname else 'YouTube'
    if 'instagram' in hostname:
        return 'Instagram'
    if 'facebook' in hostname or 'fb.watch' in hostname:
        return 'Facebook'
    return 'Unknown'


def parse_urls(raw_text: str) -> Tuple[List[str], List[str]]:
    """
    Robustly extract URLs from any raw text blob.

    Strategy: instead of splitting by lines/commas (fragile), we use a regex
    to directly find every http/https token in the text. This is immune to:
    - Extra blank lines or enters between URLs
    - Leading/trailing spaces on any line
    - Tabs, commas, or other separators
    - BOM / zero-width / non-breaking spaces
    - Mixed separators in the same paste

    Non-URL text is silently ignored. Anything that looks like a URL but
    isn't from an allowed platform is returned in invalid_raws for display.
    """
    # Strip invisible / exotic characters that can hitch-hike on clipboard pastes
    raw_text = (raw_text
                .replace('\u200b', '')   # zero-width space
                .replace('\u200c', '')   # zero-width non-joiner
                .replace('\u200d', '')   # zero-width joiner
                .replace('\ufeff', '')   # BOM
                .replace('\u00a0', ' ')  # non-breaking space → regular space
                )

    # Pull every http/https token from the blob, regardless of surrounding text
    _URL_RE = re.compile(r'https?://[^\s\t\r\n,"\'\]\[<>]+', re.IGNORECASE)
    found = _URL_RE.findall(raw_text)

    # Strip any trailing punctuation that got swept up (e.g. trailing . or )
    cleaned = []
    for u in found:
        u = re.sub(r'[.,;:!?\)\]]+$', '', u)
        if u:
            cleaned.append(u)

    # De-duplicate while preserving order
    seen: set = set()
    unique = []
    for u in cleaned:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    valid_urls  = [u for u in unique if is_valid_url(u)]
    invalid_raw = [u for u in unique if not is_valid_url(u)]

    return valid_urls, invalid_raw

    return valid_urls, invalid_raw


def get_video_info(url: str) -> dict:
    """Fetch metadata for a URL using yt-dlp."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            video_formats = [
                f for f in info.get('formats', [])
                if f.get('vcodec') != 'none' and f.get('resolution') and f.get('resolution') != 'audio only'
            ]
            resolutions = sorted(list(set([f.get('height') for f in video_formats if f.get('height')])), reverse=True)
            resolution_options = [f"{res}p" for res in resolutions if res]

            audio_formats = [
                f for f in info.get('formats', [])
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
            ]
            bitrates = sorted(list(set([f.get('abr') for f in audio_formats if f.get('abr')])), reverse=True)
            bitrate_options = [f"{int(abr)}kbps" for abr in bitrates if abr]

            return {
                'success': True,
                'title': info.get('title', 'Unknown Title'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration_string', ''),
                'resolutions': resolution_options if resolution_options else ['Default'],
                'bitrates': bitrate_options if bitrate_options else ['Default'],
            }
    except Exception as e:
        return {'success': False, 'error': str(e)}
