"""
DECIBEL — Flask backend
"""

import hashlib
import html
import http.cookiejar
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import wraps

import requests as req
import yt_dlp
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session, stream_with_context
from flask_cors import CORS
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from mutagen.id3 import APIC, ID3, TALB, TPE1, TRCK, TIT2, error as ID3Error
from mutagen.mp3 import MP3
from ytmusicapi import LikeStatus, YTMusic

# ── Environment ───────────────────────────────────────────────────────────────

# Safe for local desktop apps — traffic never leaves the machine
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
# Prevent "No translation file found" errors in PyInstaller builds
os.environ["LANG"] = "C"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

def resource_path(relative: str) -> str:
    """Resolve path for both dev and PyInstaller frozen builds."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
STATIC_FOLDER = resource_path("static")
DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Music", "Downloads")
BROWSER_JSON = os.path.join(BASE_DIR, "browser.json")
COOKIES_TXT = os.path.join(BASE_DIR, "cookies.txt")
TOKEN_FILE = os.path.join(BASE_DIR, "oauth_token.json")
CLIENT_CONFIG_FILE = os.path.join(BASE_DIR, "client_secret.json")
SECRET_KEY_FILE = os.path.join(BASE_DIR, ".flask_secret_key")

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=STATIC_FOLDER)
CORS(app, supports_credentials=True)

# Persist secret key across restarts
if os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE) as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(app.secret_key)

# ── OAuth scopes ──────────────────────────────────────────────────────────────

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtubepartner",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# ── Global state ──────────────────────────────────────────────────────────────

ytmusic: YTMusic = YTMusic()
ytmusic_lock = threading.Lock()

progress_data: dict = {}
progress_lock = threading.Lock()
PROGRESS_TTL = 300  # 5 minutes

now_playing: dict = {}

# ── yt-dlp discovery ──────────────────────────────────────────────────────────

def _find_ytdlp() -> list[str] | None:
    """Return a yt-dlp CLI command list, or None to use the Python API."""
    if getattr(sys, "frozen", False):
        candidate = os.path.join(getattr(sys, "_MEIPASS", ""), "yt-dlp.exe")
        if os.path.exists(candidate):
            return [candidate]
    else:
        venv_bin = "Scripts" if os.name == "nt" else "bin"
        exe_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
        candidate = os.path.join(BASE_DIR, "venv", venv_bin, exe_name)
        if os.path.exists(candidate):
            return [candidate]

    system = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if system:
        return [system]

    return None  # Fall back to Python API


YTDLP_CMD = _find_ytdlp()
logger.info("yt-dlp: %s", "Python API" if YTDLP_CMD is None else YTDLP_CMD[0])

# ── Locks & caches ────────────────────────────────────────────────────────────

_album_cache: dict = {}
_album_cache_lock = threading.Lock()
ALBUM_CACHE_TTL = 1800  # 30 minutes

_library_artists_cache: list | None = None
_library_artists_cache_time: float = 0
LIBRARY_CACHE_TTL = 1800  # 30 minutes

_auth_cache: dict = {"valid": None, "timestamp": 0.0}
AUTH_CACHE_TTL = 300  # 5 minutes

_rate_limits: dict = {}
_rate_limit_lock = threading.Lock()

# ── Decorators ────────────────────────────────────────────────────────────────

def validate_route_id(f):
    """Reject requests where the first URL kwarg isn't a safe ID."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        for val in kwargs.values():
            if not _valid_id(val):
                return jsonify({"error": "Invalid ID format"}), 400
        return f(*args, **kwargs)
    return wrapper


def rate_limit(max_calls: int, window: float):
    """Simple sliding-window rate limiter keyed by route name."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            now = time.time()
            key = f.__name__
            with _rate_limit_lock:
                calls = [t for t in _rate_limits.get(key, []) if now - t < window]
                if len(calls) >= max_calls:
                    return jsonify({"error": "Too many requests, slow down."}), 429
                calls.append(now)
                _rate_limits[key] = calls
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ── Helpers ───────────────────────────────────────────────────────────────────

def _valid_id(value: str, pattern=re.compile(r"^[a-zA-Z0-9_-]+$")) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return value if pattern.match(value) else None


def get_thumb(thumbnails: list | None) -> str:
    if not thumbnails:
        return ""
    return thumbnails[-1].get("url", "")


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip()
    name = os.path.basename(name)  # strip any path components
    if name.startswith("."):
        name = "_" + name[1:]
    return name or "track"


def ytmusic_call(func, *args, max_retries: int = 3, **kwargs):
    """Thread-safe YTMusic call with exponential backoff."""
    last_err = None
    for attempt in range(max_retries):
        try:
            with ytmusic_lock:
                return func(*args, **kwargs)
        except Exception as exc:
            last_err = exc
            if attempt < max_retries - 1:
                wait = attempt + 1
                logger.warning("YTMusic attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
                time.sleep(wait)
    logger.error("YTMusic gave up after %d attempts: %s", max_retries, last_err)
    raise last_err


def format_song(s: dict) -> dict:
    artists = s.get("artists") or []
    return {
        "videoId":   s.get("videoId", ""),
        "title":     s.get("title", ""),
        "artist":    ", ".join(a.get("name", "") for a in artists),
        "album":     (s.get("album") or {}).get("name", ""),
        "duration":  s.get("duration", ""),
        "thumbnail": get_thumb(s.get("thumbnails")),
    }


def format_artist(a: dict) -> dict:
    return {
        "browseId":    a.get("browseId", ""),
        "name":        a.get("artist") or a.get("name") or a.get("title", ""),
        "subscribers": a.get("subscribers", ""),
        "thumbnail":   get_thumb(a.get("thumbnails")),
    }


def format_album(a: dict) -> dict:
    artists = a.get("artists") or []
    return {
        "browseId":  a.get("browseId", ""),
        "title":     a.get("title", ""),
        "artist":    ", ".join(x.get("name", "") for x in artists),
        "year":      a.get("year", ""),
        "thumbnail": get_thumb(a.get("thumbnails")),
    }

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _generate_sapisidhash(sapisid: str, origin: str = "https://music.youtube.com") -> str:
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _init_ytmusic() -> bool:
    """Initialize YTMusic. Falls back gracefully: token → browser.json → unauthenticated."""
    global ytmusic
    for path, label in [(TOKEN_FILE, "OAuth token"), (BROWSER_JSON, "browser.json")]:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            instance = YTMusic(path) if path == BROWSER_JSON else YTMusic(headers_auth=data)
            with ytmusic_lock:
                ytmusic = instance
            logger.info("YTMusic initialised with %s", label)
            return True
        except Exception as exc:
            logger.warning("Failed to init YTMusic with %s: %s", label, exc)

    with ytmusic_lock:
        ytmusic = YTMusic()
    logger.info("YTMusic running unauthenticated")
    return False


def is_auth_valid() -> bool:
    """Check auth with a short-lived cache to avoid hammering the API."""
    now = time.time()
    if _auth_cache["valid"] is not None and (now - _auth_cache["timestamp"]) < AUTH_CACHE_TTL:
        return bool(_auth_cache["valid"])
    if not os.path.exists(BROWSER_JSON):
        _auth_cache.update(valid=False, timestamp=now)
        return False
    try:
        YTMusic(BROWSER_JSON).get_home(limit=1)
        _auth_cache.update(valid=True, timestamp=now)
        return True
    except Exception:
        _auth_cache.update(valid=False, timestamp=now)
        return False


def _clear_auth_cache():
    _auth_cache.update(valid=None, timestamp=0.0)


def _get_oauth_flow() -> Flow | None:
    if not os.path.exists(CLIENT_CONFIG_FILE):
        return None
    with open(CLIENT_CONFIG_FILE) as f:
        cfg = json.load(f)
    if "installed" in cfg:
        inst = cfg["installed"]
        cfg = {"web": {
            "client_id": inst["client_id"],
            "client_secret": inst["client_secret"],
            "auth_uri": inst["auth_uri"],
            "token_uri": inst["token_uri"],
            "auth_provider_x509_cert_url": inst["auth_provider_x509_cert_url"],
            "redirect_uris": ["http://localhost:5000/oauth/callback"],
        }}
    return Flow.from_client_config(cfg, scopes=OAUTH_SCOPES, redirect_uri="http://localhost:5000/oauth/callback")


def _get_user_info() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {"logged_in": False}
    try:
        with open(TOKEN_FILE) as f:
            info = json.load(f)
        if "email" in info:
            return {"logged_in": True, "email": info["email"], "name": info.get("name", ""), "picture": info.get("picture", "")}
    except Exception:
        pass
    return {"logged_in": False}

# ── Cookie extraction ─────────────────────────────────────────────────────────

def _extract_browser_cookies(browser_name: str) -> dict:
    """Extract YT cookies from the named browser and write browser.json + cookies.txt."""
    try:
        import browser_cookie3
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "browser-cookie3"], timeout=60)
        import browser_cookie3

    funcs = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "brave": browser_cookie3.brave,
        "opera": browser_cookie3.opera,
    }
    if browser_name not in funcs:
        return {"success": False, "error": f"Unknown browser '{browser_name}'. Supported: {', '.join(funcs)}"}

    try:
        cj = funcs[browser_name](domain_name="youtube.com")
        cookies = {
            c.name: c.value
            for c in cj
            if c.domain and ("youtube" in c.domain or "google" in c.domain)
        }
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("encrypted", "keyring", "dbus")):
            hint = f"Close {browser_name.title()} completely and try again."
        elif any(k in msg for k in ("profile", "not found")):
            hint = f"{browser_name.title()} profile not found."
        elif any(k in msg for k in ("permission", "access denied")):
            hint = "Try running DECIBEL as Administrator."
        else:
            hint = str(exc)[:200]
        return {"success": False, "error": hint}

    if not cookies:
        return {"success": False, "error": f"No YouTube cookies found in {browser_name.title()}. Log in at music.youtube.com first."}

    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        return {"success": False, "error": f"SAPISID not found — are you logged into YouTube Music in {browser_name.title()}?"}

    # Write browser.json
    browser_cfg = {
        "Accept": "*/*",
        "Authorization": _generate_sapisidhash(sapisid),
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": "https://music.youtube.com",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    with open(BROWSER_JSON, "w", encoding="utf-8") as f:
        json.dump(browser_cfg, f, indent=2, ensure_ascii=False)

    # Write cookies.txt (Netscape format for yt-dlp)
    jar = http.cookiejar.MozillaCookieJar()
    for name, value in cookies.items():
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value,
            port=None, port_specified=False,
            domain=".youtube.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True,
            secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={},
        ))
    jar.save(COOKIES_TXT, ignore_discard=True, ignore_expires=True)

    # Verify
    try:
        YTMusic(BROWSER_JSON).get_home(limit=1)
        return {"success": True, "message": f"Authenticated via {browser_name.title()}!"}
    except Exception as exc:
        return {"success": False, "error": f"Cookie extraction succeeded but verification failed: {str(exc)[:200]}"}

# ── Studio version resolution ─────────────────────────────────────────────────

_NON_STUDIO = re.compile(
    r"\b(music video|official video|official mv|live(?: at| session| version| performance)?|"
    r"acoustic(?: version| session)?|remix(?: version)?|karaoke|instrumental|"
    r"extended (mix|version)|radio edit|lyric video|visualizer|audio only)\b",
    re.IGNORECASE,
)

_FEAT = re.compile(r"\(.*?\)|\[.*?\]|\bfeat\.?\b|\bft\.?\b|\bwith\b|\s+", re.IGNORECASE)


def _normalize(title: str) -> str:
    return _FEAT.sub(" ", title.lower()).strip()


def find_studio_version(title: str, artist: str, album: str | None = None) -> str | None:
    """
    Return the videoId of the best official studio recording for (title, artist).

    Priority order:
      1. Album tracklist lookup — most reliable, directly matches official releases.
      2. Song search — fallback, scored strictly on title + artist + album match.

    Runs in a thread with a 20-second hard timeout.
    """
    result: list[str | None] = [None]

    title_lower  = title.lower().strip()
    artist_lower = artist.lower().strip() if artist else ""
    album_lower  = album.lower().strip()  if album  else ""

    def _artist_match(r: dict) -> bool:
        """True if any of the result's artists fuzzy-match our artist."""
        if not artist_lower:
            return True
        for a in (r.get("artists") or []):
            if artist_lower in a.get("name", "").lower():
                return True
        return False

    def _strategy_album() -> str | None:
        """
        Use the album tracklist to confirm the exact track title, then find
        the studio audio upload via song search.

        On the free tier, album tracklists return music video IDs — so we
        never use the videoId from the tracklist directly. Instead we use the
        confirmed title + artist to run a targeted song search.
        """
        if not album_lower or not artist_lower:
            return None

        key = f"{artist_lower}::{album_lower}"
        with _album_cache_lock:
            cached = _album_cache.get(key)

        if cached:
            tracks = cached.get("tracks", [])
        else:
            try:
                hits = ytmusic_call(ytmusic.search, f"{artist} {album}", filter="albums", limit=8)
            except Exception:
                return None

            best, best_score = None, 0
            for a in hits:
                a_title  = a.get("title", "").lower()
                a_artist = ", ".join(x.get("name", "") for x in (a.get("artists") or [])).lower()
                title_ok  = (a_title == album_lower or album_lower in a_title or a_title in album_lower)
                artist_ok = (artist_lower in a_artist or a_artist in artist_lower)
                score = (200 if a_title == album_lower else 100 if title_ok else 0) + (50 if artist_ok else 0)
                if score > best_score:
                    best_score, best = score, a

            if not best or best_score < 100 or not best.get("browseId"):
                return None
            try:
                data = ytmusic_call(ytmusic.get_album, best["browseId"])
            except Exception:
                return None

            tracks = data.get("tracks", [])
            with _album_cache_lock:
                _album_cache[key] = {
                    "browseId": best["browseId"],
                    "tracks": tracks,
                    "title": data.get("title", ""),
                    "_ts": time.time(),
                }

        # Find the confirmed track title from the album tracklist
        norm_search = _normalize(title_lower)
        confirmed_title = None
        for t in tracks:
            t_title = t.get("title", "").lower().strip()
            if not t_title:
                continue
            if t_title == title_lower or _normalize(t_title) == norm_search:
                confirmed_title = t.get("title", title)  # use original casing
                break

        if not confirmed_title:
            return None

        # Now use song search with the confirmed title to get the studio audio upload
        # (album tracklist IDs on free tier point to music videos, not studio audio)
        return _find_studio_via_song_search(confirmed_title, artist, album)

    def _find_studio_via_song_search(track_title: str, track_artist: str, track_album: str | None) -> str | None:
        """Search for the studio audio version of a track by exact title + artist."""
        query = f"{track_artist} {track_title}"
        try:
            hits = ytmusic_call(ytmusic.search, query, filter="songs", limit=20)
        except Exception:
            return None

        t_lower = track_title.lower().strip()
        a_lower = track_artist.lower().strip()
        al_lower = (track_album or "").lower().strip()
        candidates = []

        for r in hits:
            r_title  = r.get("title", "").lower().strip()
            vid      = r.get("videoId")
            if not vid:
                continue
            if _NON_STUDIO.search(r_title):
                continue

            score = 0

            # Title must match
            if r_title == t_lower:
                score += 300
            elif _normalize(r_title) == _normalize(t_lower):
                score += 250
            elif t_lower in r_title:
                score += 100
            else:
                continue

            # Artist must match — this is what filters out covers
            r_artists = [a.get("name", "").lower() for a in (r.get("artists") or [])]
            if any(a_lower in ra or ra in a_lower for ra in r_artists):
                score += 200
            else:
                score -= 400  # wrong artist = almost certainly a cover, kill it

            # Album match is a strong positive signal for official uploads
            if al_lower:
                r_album = (r.get("album") or {}).get("name", "").lower()
                if r_album == al_lower:
                    score += 200
                elif al_lower in r_album or r_album in al_lower:
                    score += 100

            if score > 200:
                candidates.append((score, vid))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return None

    def _strategy_song() -> str | None:
        """Fallback: find the studio version purely via song search."""
        return _find_studio_via_song_search(title, artist, album)

    def _run():
        # Album lookup first — it's the most reliable source of truth
        result[0] = _strategy_album() or _strategy_song()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=20)
    return result[0]

# ── ffmpeg ────────────────────────────────────────────────────────────────────

def _ensure_ffmpeg() -> bool:
    if shutil.which("ffmpeg"):
        return True
    logger.info("ffmpeg not found — attempting auto-install")
    cmds = (
        [["winget", "install", "Gyan.FFmpeg", "-y", "--accept-package-agreements", "--accept-source-agreements"],
         ["choco", "install", "ffmpeg", "-y"]]
        if os.name == "nt"
        else [["apt-get", "install", "-y", "ffmpeg"], ["brew", "install", "ffmpeg"]]
    )
    for cmd in cmds:
        try:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            r = subprocess.run(cmd, capture_output=True, timeout=120, **flags)
            if r.returncode == 0 and shutil.which("ffmpeg"):
                logger.info("ffmpeg installed via %s", cmd[0])
                return True
        except Exception:
            pass
    logger.warning("ffmpeg auto-install failed — downloads will not work")
    return False


FFMPEG_AVAILABLE = _ensure_ffmpeg()

# ── Metadata writing ──────────────────────────────────────────────────────────

def _write_metadata(path: str, title: str = "", artist: str = "", album: str = "", track_number: str | int | None = None, thumb_path: str | None = None):
    if not os.path.exists(path):
        return
    try:
        try:
            tags = ID3(path)
        except ID3Error:
            MP3(path).add_tags()
            MP3(path).save()
            tags = ID3(path)
        if title:        tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:       tags["TPE1"] = TPE1(encoding=3, text=artist)
        if album:        tags["TALB"] = TALB(encoding=3, text=album)
        if track_number: tags["TRCK"] = TRCK(encoding=3, text=str(track_number))
        if thumb_path and os.path.exists(thumb_path):
            with open(thumb_path, "rb") as f:
                tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=f.read())
        tags.save()
        return
    except Exception as exc:
        logger.warning("ID3 write failed, trying EasyID3: %s", exc)
    try:
        from mutagen.easyid3 import EasyID3
        try:
            tags = EasyID3(path)
        except ID3Error:
            MP3(path).add_tags()
            tags = EasyID3(path)
        if title:        tags["title"] = title
        if artist:       tags["artist"] = artist
        if album:        tags["album"] = album
        if track_number: tags["tracknumber"] = str(track_number)
        tags.save()
        # EasyID3 doesn't support APIC — re-open as raw ID3 to embed art
        if thumb_path and os.path.exists(thumb_path):
            try:
                raw = ID3(path)
                with open(thumb_path, "rb") as f:
                    raw["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=f.read())
                raw.save()
            except Exception:
                pass
    except Exception as exc:
        logger.error("Metadata write completely failed for %s: %s", os.path.basename(path), exc)

# ── Download ──────────────────────────────────────────────────────────────────

def _download_thumbnail(url: str) -> str | None:
    if not url:
        return None
    try:
        r = req.get(url, timeout=10)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(r.content)
        tmp.close()
        return tmp.name
    except Exception:
        return None


def _cleanup_temp(path: str | None):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _crop_thumbnail_to_square(src: str) -> str | None:
    """
    Crop a 16:9 YouTube thumbnail to a centered 1:1 square and save as jpg.
    Returns the path to the cropped file, or None if it fails.
    """
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        w, h = img.size
        size = min(w, h)
        left = (w - size) // 2
        top  = (h - size) // 2
        img  = img.crop((left, top, left + size, top + size))
        tmp  = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        img.save(tmp.name, "JPEG", quality=95)
        tmp.close()
        return tmp.name
    except Exception as exc:
        logger.warning("Thumbnail crop failed: %s", exc)
        return None


def _run_download(video_id: str, key: str, title: str, track_number, thumbnail_url: str, album: str, artist: str):
    def _set_progress(**kwargs):
        with progress_lock:
            progress_data[key] = {**progress_data.get(key, {}), "_ts": time.time(), **kwargs}

    if not FFMPEG_AVAILABLE:
        _set_progress(status="error", percent=0, error="ffmpeg not found. Run: winget install ffmpeg")
        return

    _set_progress(status="starting", percent=3, title=title)

    studio_id = find_studio_version(title, artist, album or None)
    if studio_id:
        video_id = studio_id

    url = f"https://music.youtube.com/watch?v={video_id}"

    clean_title = safe_filename(title)
    out_folder = os.path.join(DOWNLOAD_FOLDER, safe_filename(album)) if album else DOWNLOAD_FOLDER
    os.makedirs(out_folder, exist_ok=True)
    out_template = os.path.join(out_folder, f"{clean_title}.%(ext)s")

    def _progress_hook(d):
        if d["status"] == "downloading":
            try:
                pct = float(d.get("_percent_str", "0%").replace("%", "").strip())
                _set_progress(status="downloading", percent=min(int(pct), 93))
            except Exception:
                pass
        elif d["status"] == "finished":
            _set_progress(status="converting", percent=96)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            # EmbedThumbnail removed — we crop then embed manually below
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        "progress_hooks": [_progress_hook],
    }
    if os.path.exists(COOKIES_TXT):
        opts["cookiefile"] = COOKIES_TXT

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Find the actual output file — yt-dlp may sanitise the filename differently
        mp3_path = os.path.join(out_folder, f"{clean_title}.mp3")
        if not os.path.exists(mp3_path):
            # Scan folder for any mp3 with a similar name
            for f in os.listdir(out_folder):
                if f.endswith(".mp3") and clean_title[:10].lower() in f.lower():
                    mp3_path = os.path.join(out_folder, f)
                    logger.info("Found mp3 via scan: %s", f)
                    break

        logger.info("mp3=%s exists=%s", mp3_path, os.path.exists(mp3_path))

        cropped_thumb = None
        if os.path.exists(mp3_path):
            # Find the thumbnail yt-dlp wrote alongside the mp3
            raw_thumb = os.path.join(out_folder, f"{clean_title}.jpg")
            if os.path.exists(raw_thumb):
                cropped_thumb = _crop_thumbnail_to_square(raw_thumb)
                _cleanup_temp(raw_thumb)  # remove the raw 16:9 version
            _write_metadata(mp3_path, title=title, artist=artist, album=album,
                            track_number=track_number, thumb_path=cropped_thumb)
            logger.info("Metadata written for: %s", clean_title)
        else:
            logger.error("MP3 not found after download: %s", mp3_path)

        _set_progress(status="done", percent=100, folder=out_folder)
        logger.info("Download complete: %s", clean_title)
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "bot" in msg.lower():
            msg = "YouTube rate-limited. Wait 30–60 min or re-authenticate."
        elif "ffmpeg" in msg.lower():
            msg = "ffmpeg not found. Run: winget install ffmpeg"
        _set_progress(status="error", percent=0, error=msg)
        logger.error("Download failed [%s]: %s", title, msg)
    finally:
        _cleanup_temp(cropped_thumb if "cropped_thumb" in dir() else None)


def _cleanup_progress():
    """Drop completed/failed progress entries older than PROGRESS_TTL."""
    now = time.time()
    with progress_lock:
        stale = [k for k, v in progress_data.items()
                 if v.get("status") in ("done", "error") and now - v.get("_ts", now) > PROGRESS_TTL]
        for k in stale:
            del progress_data[k]


def _start_cleanup_thread():
    def loop():
        while True:
            time.sleep(300)
            _cleanup_progress()
    threading.Thread(target=loop, daemon=True).start()

# ── Streaming ─────────────────────────────────────────────────────────────────

def _get_stream_url(video_id: str) -> str:
    """Return a direct audio URL for the given video_id."""
    yt_url = f"https://music.youtube.com/watch?v={video_id}"
    # ios_creator gives non-expiring URLs; mweb/tv_embedded are fallbacks
    clients = ["ios_creator", "ios", "tv_embedded", "mweb", "web"]

    cookie_args = []
    if os.path.exists(COOKIES_TXT):
        cookie_args = ["--cookies", COOKIES_TXT]

    base_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios_creator"]}},
    }
    if os.path.exists(COOKIES_TXT):
        base_opts["cookiefile"] = COOKIES_TXT

    last_err = None
    for client in clients:
        # Python API path
        if YTDLP_CMD is None:
            try:
                opts = {**base_opts, "extractor_args": {"youtube": {"player_client": [client]}}}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(yt_url, download=False)
                    url = (info or {}).get("url", "")
                    if url.startswith("http"):
                        logger.info("Stream URL via Python API client=%s", client)
                        return url
            except Exception as exc:
                last_err = exc
                continue

        # CLI path
        else:
            cmd = YTDLP_CMD + [
                yt_url, "--get-url", "--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "--extractor-args", f"youtube:player_client={client}",
            ] + cookie_args
            try:
                flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **flags)
                url = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
                if r.returncode == 0 and url.startswith("http"):
                    logger.info("Stream URL via CLI client=%s", client)
                    return url
                last_err = r.stderr.strip()[:200]
            except subprocess.TimeoutExpired:
                last_err = f"timeout on client={client}"
                continue

    raise RuntimeError(f"All yt-dlp clients failed. Last error: {last_err}")

# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if request.path.startswith(("/api", "/setup", "/oauth")):
        response.headers["Cache-Control"] = "no-store"
    return response

# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    page = "index.html" if is_auth_valid() else "setup.html"
    return send_from_directory(STATIC_FOLDER, page)


@app.route("/setup")
def setup_page():
    return send_from_directory(STATIC_FOLDER, "setup.html")

# ── Auth: browser cookie flow ─────────────────────────────────────────────────

@app.route("/setup/authenticate", methods=["POST"])
def setup_authenticate():
    data = request.get_json() or {}
    browser = data.get("browser", "").strip().lower()
    if not browser:
        return jsonify({"success": False, "error": "Select a browser first."}), 400

    result = _extract_browser_cookies(browser)
    if not result["success"]:
        return jsonify(result)

    _init_ytmusic()

    # Only mark cache valid after verification passes
    if is_auth_valid():
        _auth_cache.update(valid=True, timestamp=time.time())
        return jsonify(result)

    return jsonify({"success": False, "error": "Cookies extracted but verification failed — please try again."})


@app.route("/setup/status")
def setup_status():
    return jsonify({"authenticated": is_auth_valid()})

# ── Auth: OAuth (legacy, for backward compat) ─────────────────────────────────

@app.route("/oauth/login")
def oauth_login():
    if not os.path.exists(CLIENT_CONFIG_FILE):
        return jsonify({"error": "OAuth not configured.", "setup_required": True}), 400
    try:
        flow = _get_oauth_flow()
        url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        session["oauth_state"] = state
        return jsonify({"authorization_url": url})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/oauth/callback")
def oauth_callback():
    _ok = "<h2 style='color:#1db954'>✅ Login successful! You can close this window.</h2><script>window.close()</script>"
    _fail = lambda msg: f"<h2 style='color:#f44'>❌ Login failed</h2><p>{html.escape(msg)}</p><script>setTimeout(()=>window.close(),5000)</script>"
    try:
        if not session.get("oauth_state"):
            return redirect("/?error=invalid_state")
        flow = _get_oauth_flow()
        if not flow:
            return redirect("/?error=oauth_not_configured")
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        id_info = id_token.verify_oauth2_token(creds.id_token, google_requests.Request(), flow.client_config["client_id"])
        # Save token — do NOT persist client_secret
        token = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "scopes": list(creds.scopes or []),
            "email": id_info.get("email", ""),
            "name": id_info.get("name", ""),
            "picture": id_info.get("picture", ""),
        }
        with open(TOKEN_FILE, "w") as f:
            json.dump(token, f, indent=2)
        session.pop("oauth_state", None)
        _init_ytmusic()
        return _ok
    except Exception as exc:
        return _fail(str(exc))


@app.route("/oauth/logout", methods=["POST"])
def oauth_logout():
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        _init_ytmusic()
        _clear_auth_cache()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/oauth/status")
def oauth_status():
    info = _get_user_info()
    info["oauth_configured"] = os.path.exists(CLIENT_CONFIG_FILE)
    return jsonify(info)

# ── Home & library ────────────────────────────────────────────────────────────

@app.route("/home")
def home():
    try:
        data = ytmusic_call(ytmusic.get_home, limit=6)
        sections = []
        for section in data:
            title = section.get("title", "")
            if "shows" in title.lower() and "for you" in title.lower():
                continue
            items = []
            for r in section.get("contents", []):
                if r.get("videoId"):
                    items.append(format_song(r))
                elif r.get("browseId") and r.get("artists"):
                    items.append({"type": "album", **format_album(r)})
                elif r.get("browseId"):
                    items.append({"type": "artist", **format_artist(r)})
            if items:
                sections.append({"title": title, "items": items[:8]})
        return jsonify({"sections": sections})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/library/artists")
def library_artists():
    global _library_artists_cache, _library_artists_cache_time
    try:
        page = int(request.args.get("page", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid page"}), 400

    size = 80
    force = request.args.get("refresh", "").lower() == "true"
    try:
        now = time.time()
        if force or _library_artists_cache is None or (now - _library_artists_cache_time) > LIBRARY_CACHE_TTL:
            data = ytmusic_call(ytmusic.get_library_artists, limit=500)
            _library_artists_cache = [format_artist(a) for a in data]
            _library_artists_cache_time = now
        start, end = page * size, (page + 1) * size
        return jsonify({"artists": _library_artists_cache[start:end], "total": len(_library_artists_cache), "page": page, "has_more": end < len(_library_artists_cache)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/library/playlists")
def library_playlists():
    try:
        data = ytmusic_call(ytmusic.get_library_playlists, limit=50)
        return jsonify({"playlists": [{"playlistId": p.get("playlistId", ""), "title": p.get("title", ""), "count": p.get("count", ""), "thumbnail": get_thumb(p.get("thumbnails"))} for p in data]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/playlist/<playlist_id>")
@validate_route_id
def playlist_page(playlist_id: str):
    try:
        data = ytmusic_call(ytmusic.get_playlist, playlist_id, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({"title": data.get("title", ""), "author": (data.get("author") or {}).get("name", ""), "count": data.get("trackCount", len(tracks)), "thumbnail": get_thumb(data.get("thumbnails")), "tracks": tracks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── Search & browse ───────────────────────────────────────────────────────────

@app.route("/search")
@rate_limit(max_calls=30, window=60)
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    try:
        songs   = ytmusic_call(ytmusic.search, query, filter="songs",   limit=8)
        artists = ytmusic_call(ytmusic.search, query, filter="artists", limit=8)
        albums  = ytmusic_call(ytmusic.search, query, filter="albums",  limit=4)
        return jsonify({"songs": [format_song(s) for s in songs], "artists": [format_artist(a) for a in artists], "albums": [format_album(a) for a in albums]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/artist/<browse_id>")
@validate_route_id
def artist_page(browse_id: str):
    try:
        data   = ytmusic_call(ytmusic.get_artist, browse_id)
        songs  = [format_song(s) for s in (data.get("songs", {}).get("results") or [])[:10]]
        alb    = data.get("albums", {})
        if alb.get("browseId") and alb.get("params"):
            try:
                albums = [format_album(a) for a in ytmusic_call(ytmusic.get_artist_albums, alb["browseId"], alb["params"])]
            except Exception:
                albums = [format_album(a) for a in (alb.get("results") or [])]
        else:
            albums = [format_album(a) for a in (alb.get("results") or [])]
        return jsonify({"name": data.get("name", ""), "thumbnail": get_thumb(data.get("thumbnails")), "subscribers": data.get("subscribers", ""), "songs": songs, "albums": albums})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/album/<browse_id>")
@validate_route_id
@rate_limit(max_calls=20, window=60)
def album_page(browse_id: str):
    try:
        data        = ytmusic_call(ytmusic.get_album, browse_id)
        artists     = data.get("artists") or []
        album_artist= ", ".join(a.get("name", "") for a in artists)
        album_title = data.get("title", "")
        album_thumbnail = get_thumb(data.get("thumbnails"))
        tracks = []
        for t in data.get("tracks", []):
            t_artist = ", ".join(a.get("name", "") for a in (t.get("artists") or [])) or album_artist
            studio_id = find_studio_version(t.get("title", ""), t_artist, album_title) or t.get("videoId")
            # Use per-track thumbnail if available, fall back to album art
            t_thumb = get_thumb(t.get("thumbnails")) or album_thumbnail
            tracks.append({"videoId": studio_id, "title": t.get("title", ""), "duration": t.get("duration", ""), "trackNumber": t.get("trackNumber"), "artists": t.get("artists", []), "thumbnail": t_thumb})
        return jsonify({"title": album_title, "artist": album_artist, "year": data.get("year", ""), "thumbnail": album_thumbnail, "tracks": tracks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/album/<browse_id>/resolve-studio", methods=["POST"])
@validate_route_id
def resolve_album_studio(browse_id: str):
    try:
        data        = ytmusic_call(ytmusic.get_album, browse_id)
        album_title = data.get("title", "")
        artists     = data.get("artists") or []
        album_artist= ", ".join(a.get("name", "") for a in artists)
        tracks      = data.get("tracks", [])
        key = album_title.lower().strip()
        with _album_cache_lock:
            _album_cache[key] = {"browseId": browse_id, "tracks": tracks, "title": album_title, "_ts": time.time()}
        mapping = {}
        for t in tracks:
            orig = t.get("videoId")
            t_artist = ", ".join(a.get("name", "") for a in (t.get("artists") or [])) or album_artist
            if orig and t.get("title"):
                studio = find_studio_version(t["title"], t_artist, album_title)
                mapping[orig] = studio or orig
        return jsonify({"success": True, "album": album_title, "artist": album_artist, "studio_mapping": mapping})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── Download ──────────────────────────────────────────────────────────────────

@app.route("/download", methods=["POST"])
@rate_limit(max_calls=10, window=60)
def start_download():
    data     = request.get_json() or {}
    video_id = data.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "No videoId"}), 400
    if not _valid_id(video_id):
        return jsonify({"error": "Invalid videoId"}), 400

    key = f"{video_id}_{uuid.uuid4().hex[:8]}"
    with progress_lock:
        progress_data[key] = {"status": "starting", "percent": 0, "title": data.get("title", ""), "_ts": time.time()}

    threading.Thread(
        target=_run_download,
        args=(video_id, key, data.get("title", "Track"), data.get("trackNumber"), data.get("thumbnail", ""), data.get("album", ""), data.get("artist", "")),
        daemon=True,
    ).start()
    return jsonify({"status": "started", "key": key})


@app.route("/progress")
def get_progress():
    key = request.args.get("key", "")
    _cleanup_progress()
    with progress_lock:
        return jsonify(progress_data.get(key, {"status": "unknown", "percent": 0}))


@app.route("/folder")
def get_folder():
    return jsonify({"folder": DOWNLOAD_FOLDER})

# ── Streaming ─────────────────────────────────────────────────────────────────

@app.route("/stream/<video_id>")
@validate_route_id
def stream_audio(video_id: str):
    global now_playing
    title  = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    album  = request.args.get("album", "").strip()

    if title and artist:
        try:
            sid = find_studio_version(title, artist, album or None)
            if sid:
                video_id = sid
        except Exception as exc:
            logger.warning("Studio resolution failed during stream: %s", exc)

    try:
        stream_url = _get_stream_url(video_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    with progress_lock:
        now_playing = {"videoId": video_id, "stream_url": stream_url}

    def _generate():
        current_url = stream_url
        offset = 0
        for attempt in range(5):
            try:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with req.get(current_url, stream=True, timeout=120, headers=headers) as r:
                    if r.status_code == 403:
                        # URL expired — re-fetch a fresh one
                        logger.warning("Stream 403 on attempt %d, re-fetching URL", attempt + 1)
                        current_url = _get_stream_url(video_id)
                        time.sleep(1)
                        continue
                    if r.status_code not in (200, 206):
                        logger.warning("Stream HTTP %d on attempt %d", r.status_code, attempt + 1)
                        time.sleep(attempt + 1)
                        continue
                    for chunk in r.iter_content(chunk_size=16384):
                        if chunk:
                            offset += len(chunk)
                            yield chunk
                    return
            except (req.exceptions.ConnectionError, req.exceptions.ReadTimeout):
                time.sleep(attempt + 1)
        logger.warning("Stream gave up after 5 retries: %s", video_id)

    try:
        ct_resp = req.head(stream_url, timeout=10)
        content_type = ct_resp.headers.get("Content-Type", "audio/mp4")
    except Exception:
        content_type = "audio/mp4"
    return Response(stream_with_context(_generate()), mimetype=content_type, headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"})


@app.route("/now_playing")
def get_now_playing():
    return jsonify(now_playing)


@app.route("/api/watch/playlist")
def watch_playlist():
    video_id = request.args.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "No videoId"}), 400
    try:
        data   = ytmusic_call(ytmusic.get_watch_playlist, video_id, limit=25)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({"tracks": tracks, "lyrics": data.get("lyrics")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── Playlist management ───────────────────────────────────────────────────────

@app.route("/api/playlists")
def get_playlists():
    try:
        data = ytmusic_call(ytmusic.get_library_playlists, limit=100)
        return jsonify({"playlists": [{"playlistId": p.get("playlistId", ""), "title": p.get("title", ""), "count": p.get("count", 0), "thumbnail": get_thumb(p.get("thumbnails"))} for p in data]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/<playlist_id>")
@validate_route_id
def get_playlist(playlist_id: str):
    try:
        data   = ytmusic_call(ytmusic.get_playlist, playlist_id, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({"playlistId": playlist_id, "title": data.get("title", ""), "description": data.get("description", ""), "author": (data.get("author") or {}).get("name", ""), "count": data.get("trackCount", len(tracks)), "thumbnail": get_thumb(data.get("thumbnails")), "tracks": tracks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/create", methods=["POST"])
def create_playlist():
    data  = request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400
    try:
        pid = ytmusic_call(ytmusic.create_playlist, title=title, description=data.get("description", ""), privacy_status=data.get("privacy", "PRIVATE").upper())
        return jsonify({"success": True, "playlistId": pid, "title": title})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/<playlist_id>/delete", methods=["POST"])
@validate_route_id
def delete_playlist(playlist_id: str):
    try:
        ytmusic_call(ytmusic.delete_playlist, playlist_id)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/<playlist_id>/add", methods=["POST"])
@validate_route_id
def add_to_playlist(playlist_id: str):
    data     = request.get_json() or {}
    ids = data.get("videoIds", [])
    if not ids:
        return jsonify({"error": "No videoIds"}), 400
    try:
        ytmusic_call(ytmusic.add_playlist_items, playlistId=playlist_id, videoIds=ids, duplicates=False)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/<playlist_id>/remove", methods=["POST"])
@validate_route_id
def remove_from_playlist(playlist_id: str):
    data   = request.get_json() or {}
    videos = data.get("videos", [])
    if not videos:
        return jsonify({"error": "No videos"}), 400
    try:
        ytmusic_call(ytmusic.remove_playlist_items, playlistId=playlist_id, videos=videos)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── Song ratings ──────────────────────────────────────────────────────────────

_RATING_MAP = {"LIKE": LikeStatus.LIKE, "DISLIKE": LikeStatus.DISLIKE, "INDIFFERENT": LikeStatus.INDIFFERENT}


@app.route("/api/song/rate", methods=["POST"])
def rate_song():
    data     = request.get_json() or {}
    video_id = data.get("videoId", "").strip()
    rating   = data.get("rating", "INDIFFERENT").upper()
    if not video_id:
        return jsonify({"error": "No videoId"}), 400
    try:
        ytmusic_call(ytmusic.rate_song, videoId=video_id, rating=_RATING_MAP.get(rating, LikeStatus.INDIFFERENT))
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/song/rating")
def get_song_rating():
    video_id = request.args.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "No videoId"}), 400
    try:
        data  = ytmusic_call(ytmusic.get_watch_playlist, video_id, limit=1)
        track = (data.get("tracks") or [{}])[0]
        return jsonify({"videoId": video_id, "likeStatus": track.get("likeStatus", "INDIFFERENT")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playlist/liked")
def get_liked_songs():
    try:
        data   = ytmusic_call(ytmusic.get_liked_songs, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({"playlistId": data.get("id", "LM"), "title": data.get("name", "Liked Songs"), "count": data.get("trackCount", len(tracks)), "thumbnail": get_thumb(data.get("thumbnails")), "tracks": tracks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── Startup ───────────────────────────────────────────────────────────────────

_init_ytmusic()
_start_cleanup_thread()

if __name__ == "__main__":
    logger.info("DECIBEL running at http://127.0.0.1:5000")
    logger.info("Downloads → %s", DOWNLOAD_FOLDER)
    app.run(host="127.0.0.1", port=5000, debug=False)