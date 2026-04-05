import os
import re
import sys
import time
import shutil
import threading
import subprocess
import tempfile
import json
import secrets
import logging
import base64
import hashlib
import requests as req

# Allow OAuth to work on localhost (HTTP) for local development
# This is safe for desktop apps where traffic never leaves the machine
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Disable locale to avoid gettext issues in PyInstaller builds
# This prevents "No translation file found" errors
os.environ["LANG"] = "C"

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, session, redirect, url_for
from flask_cors import CORS
from ytmusicapi import YTMusic, LikeStatus
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, error as ID3Error
from mutagen.mp3 import MP3
from google_auth_oauthlib.flow import Flow
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

# ─── Logging Setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

# Secret key persistence across restarts
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret_key")
if os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "r") as f:
        app.secret_key = f.read().strip()
else:
    secret_key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(secret_key)
    app.secret_key = secret_key

CORS(app, supports_credentials=True)

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Music", "Downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# OAuth configuration
# Use executable directory for frozen apps, script directory otherwise
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_CONFIG_FILE = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(BASE_DIR, "oauth_token.json")

# OAuth scopes for YouTube Music
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtubepartner",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Initialize YTMusic (will be updated after OAuth login)
ytmusic = YTMusic()
progress_data = {}

# In-memory session store (for production, use a proper session store)
user_sessions = {}

# ─── yt-dlp Setup ─────────────────────────────────────────────────────────────

# For frozen apps, yt-dlp.exe is bundled alongside the .exe
# For development, use the venv binary or system yt-dlp
if getattr(sys, 'frozen', False):
    # In frozen environment, yt-dlp.exe is bundled in _MEIPASS
    if hasattr(sys, '_MEIPASS'):
        _frozen_ytdlp = os.path.join(sys._MEIPASS, "yt-dlp.exe")
        if os.path.exists(_frozen_ytdlp):
            YTDLP_CMD = [_frozen_ytdlp]
            logger.info(f"✅ Using bundled yt-dlp.exe")
        else:
            # Fallback to module approach
            YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]
            logger.info("⚠️ Bundled yt-dlp.exe not found, using module fallback")
    else:
        YTDLP_CMD = ["yt-dlp"]
else:
    # In development, use the venv binary or system yt-dlp
    if os.name == "nt":  # Windows
        _venv_ytdlp = os.path.join(BASE_DIR, "venv", "Scripts", "yt-dlp.exe")
    else:  # Linux/macOS
        _venv_ytdlp = os.path.join(BASE_DIR, "venv", "bin", "yt-dlp")
    
    if os.path.exists(_venv_ytdlp):
        YTDLP_CMD = [_venv_ytdlp]
    else:
        YTDLP_CMD = ["yt-dlp"]

# Validate yt-dlp module is available
try:
    import yt_dlp
    logger.info(f"✅ yt-dlp module available")
except ImportError:
    logger.error("❌ yt-dlp module not found!")
    sys.exit(1)

# Thread lock for progress data and global state
progress_lock = threading.Lock()
ytmusic_lock = threading.Lock()


# ─── Thread-Safe YTMusic Wrapper with Retry Logic ────────────────────────────

def safe_ytmusic_call(func, *args, max_retries=3, **kwargs):
    """Execute a ytmusic call with thread safety and automatic retry."""
    last_error = None
    for attempt in range(max_retries):
        try:
            with ytmusic_lock:
                return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # Exponential backoff
                logger.warning(f"YTMusic call failed (attempt {attempt+1}/{max_retries}): {e}")
            else:
                logger.error(f"YTMusic call failed after {max_retries} attempts: {e}")
    raise last_error

# Current playing track info (for streaming state)
now_playing = {}

# ─── Input Validation ─────────────────────────────────────────────────────────

def validate_id(param, pattern=r'^[a-zA-Z0-9_-]+$'):
    """Validate ID parameters to prevent injection attacks."""
    if not param or not isinstance(param, str):
        return None
    if not re.match(pattern, param):
        return None
    return param


# ─── Browser Cookie Authentication (Setup Flow) ───────────────────────────────

def generate_sapisidhash(sapisid, origin="https://music.youtube.com"):
    """Generate SAPISIDHASH header value from SAPISID cookie."""
    timestamp = int(time.time())
    hash_input = f"{timestamp} {sapisid} {origin}"
    hash_value = hashlib.sha1(hash_input.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{hash_value}"


def extract_browser_cookies(browser_name):
    """Extract cookies from browser and create auth files."""
    try:
        import browser_cookie3
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "browser_cookie3"])
        import browser_cookie3

    browsers = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "brave": browser_cookie3.brave,
        "opera": browser_cookie3.opera,
    }

    if browser_name.lower() not in browsers:
        return {"success": False, "error": f"Unknown browser: {browser_name}"}

    try:
        cj = browsers[browser_name.lower()](domain_name="youtube.com")
        cookies = {}
        for cookie in cj:
            if cookie.domain and ('youtube' in cookie.domain or 'google' in cookie.domain):
                cookies[cookie.name] = cookie.value

        if not cookies:
            return {"success": False, "error": f"No YouTube cookies found in {browser_name}. Make sure you're logged into music.youtube.com"}

        # Create browser.json
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        auth_header = generate_sapisidhash(sapisid) if sapisid else ""

        cookie_parts = [f"{name}={value}" for name, value in cookies.items()]
        browser_config = {
            "Accept": "*/*",
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": "https://music.youtube.com",
            "Cookie": "; ".join(cookie_parts),
        }

        browser_json_path = os.path.join(BASE_DIR, "browser.json")
        with open(browser_json_path, "w", encoding="utf-8") as f:
            json.dump(browser_config, f, indent=2, ensure_ascii=False)

        # Create cookies.txt
        import http.cookiejar
        cookies_txt_path = os.path.join(BASE_DIR, "cookies.txt")
        cookie_jar = http.cookiejar.MozillaCookieJar()
        for name, value in cookies.items():
            c = http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=".music.youtube.com", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True,
                secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={},
            )
            cookie_jar.set_cookie(c)
        cookie_jar.save(cookies_txt_path, ignore_discard=True, ignore_expires=True)

        # Verify auth works
        try:
            from ytmusicapi import YTMusic
            ytmusic_test = YTMusic(browser_json_path)
            ytmusic_test.get_home(limit=1)
            logger.info("✅ Browser authentication verified successfully")
        except Exception as e:
            logger.warning(f"⚠️  Auth verification failed: {e}")

        return {"success": True, "message": f"Authentication setup complete using {browser_name.title()}!"}

    except Exception as e:
        return {"success": False, "error": f"Failed to extract cookies: {str(e)}"}


@app.route("/setup/authenticate", methods=["POST"])
def setup_authenticate():
    """Authenticate using browser cookies."""
    data = request.get_json()
    browser = data.get("browser", "").lower()
    
    if not browser:
        return jsonify({"success": False, "error": "Please select a browser"}), 400

    result = extract_browser_cookies(browser)
    
    if result["success"]:
        # Reinitialize YTMusic with new auth
        initialize_ytmusic_with_token()
    
    return jsonify(result)


@app.route("/setup/status")
def setup_status():
    """Check if setup is needed."""
    browser_json = os.path.join(BASE_DIR, "browser.json")
    if os.path.exists(browser_json):
        try:
            from ytmusicapi import YTMusic
            ytmusic_test = YTMusic(browser_json)
            ytmusic_test.get_home(limit=1)
            return jsonify({"authenticated": True})
        except Exception:
            pass
    return jsonify({"authenticated": False})


# ─── OAuth 2.0 Authentication (Legacy - Keeping for backward compat) ─────────

def get_oauth_flow():
    """Create and return OAuth flow object."""
    if not os.path.exists(CLIENT_CONFIG_FILE):
        return None
    with open(CLIENT_CONFIG_FILE, "r") as f:
        client_config = json.load(f)

    # Treat "installed" app as "web" app to avoid PKCE requirement
    # This is safe for local desktop app development
    if "installed" in client_config:
        installed = client_config["installed"]
        web_config = {
            "web": {
                "client_id": installed["client_id"],
                "client_secret": installed["client_secret"],
                "auth_uri": installed["auth_uri"],
                "token_uri": installed["token_uri"],
                "auth_provider_x509_cert_url": installed["auth_provider_x509_cert_url"],
                "redirect_uris": ["http://localhost:5000/oauth/callback"],
            }
        }
        flow = Flow.from_client_config(
            web_config,
            scopes=SCOPES,
            redirect_uri="http://localhost:5000/oauth/callback",
        )
    else:
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri="http://localhost:5000/oauth/callback",
        )

    return flow


def initialize_ytmusic_with_token():
    """Initialize YTMusic with OAuth token if available."""
    global ytmusic
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                token_info = json.load(f)
            new_ytmusic = YTMusic(headers_auth=token_info)
            with ytmusic_lock:
                ytmusic = new_ytmusic
            logger.info("✅ YTMusic initialized with OAuth token")
            return True
        except Exception as e:
            logger.warning(f"⚠️  Failed to initialize YTMusic with token: {e}")
    # Fall back to browser.json if exists
    browser_file = os.path.join(BASE_DIR, "browser.json")
    if os.path.exists(browser_file):
        try:
            new_ytmusic = YTMusic(browser_file)
            with ytmusic_lock:
                ytmusic = new_ytmusic
            logger.info("✅ YTMusic initialized with browser.json")
            return True
        except Exception as e:
            logger.warning(f"⚠️  Failed to initialize YTMusic with browser.json: {e}")
    # Unauthenticated
    new_ytmusic = YTMusic()
    with ytmusic_lock:
        ytmusic = new_ytmusic
    logger.info("ℹ️  YTMusic running in unauthenticated mode")
    return False


def get_user_info():
    """Get current user info from stored token."""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                token_info = json.load(f)
            # Try to get user info from token
            if "email" in token_info:
                return {
                    "email": token_info.get("email", ""),
                    "name": token_info.get("name", ""),
                    "picture": token_info.get("picture", ""),
                    "logged_in": True,
                }
        except Exception:
            pass
    return {"logged_in": False}


@app.route("/oauth/login")
def oauth_login():
    """Start OAuth login flow."""
    if not os.path.exists(CLIENT_CONFIG_FILE):
        return jsonify({
            "error": "OAuth not configured. Please create client_secret.json",
            "setup_required": True,
        }), 400
    
    try:
        flow = get_oauth_flow()
        if not flow:
            return jsonify({"error": "Failed to create OAuth flow"}), 500
        
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        
        # Store state for verification
        session["oauth_state"] = state
        
        return jsonify({
            "authorization_url": authorization_url,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/oauth/callback")
def oauth_callback():
    """Handle OAuth callback from Google."""
    try:
        state = session.get("oauth_state")
        if not state:
            return redirect("/?error=invalid_state")
        
        flow = get_oauth_flow()
        if not flow:
            return redirect("/?error=oauth_not_configured")
        
        # Exchange authorization code for tokens
        flow.fetch_token(authorization_response=request.url)
        
        # Get credentials
        credentials = flow.credentials
        
        # Get user info
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            flow.client_config["client_id"]
        )
        
        # Save token info
        token_info = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
            "email": id_info.get("email", ""),
            "name": id_info.get("name", ""),
            "picture": id_info.get("picture", ""),
        }
        
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_info, f, indent=2)

        # Reinitialize YTMusic with new token
        initialize_ytmusic_with_token()

        # Clear session state
        session.pop("oauth_state", None)

        # Return success page that auto-closes the popup
        return '''
        <!DOCTYPE html>
        <html>
        <head><title>Login Successful</title></head>
        <body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;">
            <div style="text-align:center;">
                <h2 style="color:#1db954;">✅ Login Successful!</h2>
                <p>You can close this window.</p>
                <script>window.close();</script>
            </div>
        </body>
        </html>
        '''
    except Exception as e:
        import html
        safe_error = html.escape(str(e))
        return f'''
        <!DOCTYPE html>
        <html>
        <head><title>Login Error</title></head>
        <body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;">
            <div style="text-align:center;">
                <h2 style="color:#ff4444;">❌ Login Failed</h2>
                <p>{safe_error}</p>
                <p>You can close this window.</p>
                <script>setTimeout(() => window.close(), 5000);</script>
            </div>
        </body>
        </html>
        '''


@app.route("/oauth/logout", methods=["POST"])
def oauth_logout():
    """Logout and remove stored token."""
    global ytmusic
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)

        # Reset to unauthenticated YTMusic
        browser_file = os.path.join(BASE_DIR, "browser.json")
        if os.path.exists(browser_file):
            new_ytmusic = YTMusic(browser_file)
        else:
            new_ytmusic = YTMusic()
        
        with ytmusic_lock:
            ytmusic = new_ytmusic

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/oauth/status")
def oauth_status():
    """Get current OAuth login status."""
    user_info = get_user_info()
    user_info["oauth_configured"] = os.path.exists(CLIENT_CONFIG_FILE)
    return jsonify(user_info)


# Initialize YTMusic on startup
initialize_ytmusic_with_token()


# ─── Home / Library ───────────────────────────────────────────────────────────

@app.route("/home")
def home():
    try:
        data = safe_ytmusic_call(ytmusic.get_home, limit=6)
        sections = []
        for section in data:
            title   = section.get("title", "")
            # Skip "Shows For You" section
            if "shows" in title.lower() and "for you" in title.lower():
                continue
            results = section.get("contents", [])
            items   = []
            for r in results:
                if r.get("videoId"):
                    items.append(format_song(r))
                elif r.get("browseId") and r.get("artists"):
                    items.append({"type": "album", **format_album(r)})
                elif r.get("browseId"):
                    items.append({"type": "artist", **format_artist(r)})
            if items:
                sections.append({"title": title, "items": items[:8]})
        return jsonify({"sections": sections})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Cache all library artists on first load with TTL
_library_artists_cache = None
_library_artists_cache_time = 0
LIBRARY_CACHE_TTL = 1800  # 30 minutes

@app.route("/library/artists")
def library_artists():
    global _library_artists_cache, _library_artists_cache_time
    page   = int(request.args.get("page", 0))
    size   = 80
    force_refresh = request.args.get("refresh", "").lower() == "true"
    try:
        # Check if cache is stale or refresh is requested
        now = time.time()
        if force_refresh or _library_artists_cache is None or (now - _library_artists_cache_time) > LIBRARY_CACHE_TTL:
            data = safe_ytmusic_call(ytmusic.get_library_artists, limit=500)
            logger.info(f"Library artists fetched: {len(data)}")
            _library_artists_cache = [format_artist(a) for a in data]
            _library_artists_cache_time = now
        artists  = _library_artists_cache
        start    = page * size
        end      = start + size
        return jsonify({
            "artists":  artists[start:end],
            "total":    len(artists),
            "page":     page,
            "has_more": end < len(artists),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/library/playlists")
def library_playlists():
    try:
        data = safe_ytmusic_call(ytmusic.get_library_playlists, limit=50)
        playlists = []
        for p in data:
            playlists.append({
                "playlistId": p.get("playlistId", ""),
                "title":      p.get("title", ""),
                "count":      p.get("count", ""),
                "thumbnail":  get_thumb(p.get("thumbnails")),
            })
        return jsonify({"playlists": playlists})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/<playlist_id>")
def playlist_page(playlist_id):
    # Validate playlist_id
    if not validate_id(playlist_id):
        return jsonify({"error": "Invalid playlist ID format"}), 400
    
    try:
        data = safe_ytmusic_call(ytmusic.get_playlist, playlist_id, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({
            "title":     data.get("title", ""),
            "author":    data.get("author", {}).get("name", "") if data.get("author") else "",
            "count":     data.get("trackCount", len(tracks)),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "tracks":    tracks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Search ───────────────────────────────────────────────────────────────────

@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    try:
        songs   = safe_ytmusic_call(ytmusic.search, query, filter="songs", limit=8)
        artists = safe_ytmusic_call(ytmusic.search, query, filter="artists", limit=8)
        albums  = safe_ytmusic_call(ytmusic.search, query, filter="albums", limit=4)
        return jsonify({
            "songs":   [format_song(s)   for s in songs],
            "artists": [format_artist(a) for a in artists],
            "albums":  [format_album(a)  for a in albums],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/artist/<browse_id>")
def artist_page(browse_id):
    # Validate browse_id
    if not validate_id(browse_id):
        return jsonify({"error": "Invalid artist ID format"}), 400
    
    try:
        data   = safe_ytmusic_call(ytmusic.get_artist, browse_id)
        songs  = [format_song(s) for s in (data.get("songs", {}).get("results") or [])[:10]]
        albums_preview = (data.get("albums", {}).get("results") or [])
        albums_params  = data.get("albums", {}).get("params")
        albums_id      = data.get("albums", {}).get("browseId")
        if albums_id and albums_params:
            try:
                full   = safe_ytmusic_call(ytmusic.get_artist_albums, albums_id, albums_params)
                albums = [format_album(a) for a in full]
            except Exception:
                albums = [format_album(a) for a in albums_preview]
        else:
            albums = [format_album(a) for a in albums_preview]
        return jsonify({
            "name":        data.get("name", ""),
            "thumbnail":   get_thumb(data.get("thumbnails")),
            "subscribers": data.get("subscribers", ""),
            "songs":       songs,
            "albums":      albums,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/album/<browse_id>")
def album_page(browse_id):
    # Validate browse_id
    if not validate_id(browse_id):
        return jsonify({"error": "Invalid album ID format"}), 400
    
    try:
        data    = safe_ytmusic_call(ytmusic.get_album, browse_id)
        artists = data.get("artists") or []
        artist  = ", ".join(a.get("name", "") for a in artists)
        tracks  = []
        for t in data.get("tracks", []):
            tracks.append({
                "videoId":     t.get("videoId"),
                "title":       t.get("title", ""),
                "duration":    t.get("duration", ""),
                "trackNumber": t.get("trackNumber"),
            })
        return jsonify({
            "title":     data.get("title", ""),
            "artist":    artist,
            "year":      data.get("year", ""),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "tracks":    tracks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_thumb(thumbnails):
    if not thumbnails:
        return ""
    return thumbnails[-1].get("url", "")


def safe_filename(name):
    """Strip characters not allowed in filenames on Windows and Linux."""
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip()
    # Prevent path traversal
    if name.startswith('.') or '..' in name:
        name = name.replace('.', '_')
    return name


def download_thumbnail(url):
    """Download thumbnail to a temp file, return path or None."""
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


def write_metadata(filepath, title=None, artist=None, album=None, track_number=None):
    """Write ID3 metadata directly into MP3 using mutagen.
    Tries multiple approaches for maximum compatibility.
    """
    if not filepath or not os.path.exists(filepath):
        logger.warning(f"⚠️  Metadata write skipped: file not found: {filepath}")
        return

    approaches_tried = 0

    # Approach 1: Open existing ID3 tags or create new ones
    try:
        try:
            audio = ID3(filepath)
        except ID3Error:
            # No ID3 tags — create them
            try:
                mp3 = MP3(filepath)
                mp3.add_tags()
                mp3.save()
                audio = ID3(filepath)
            except Exception as e:
                logger.warning(f"⚠️  Metadata: Failed to create tags with MP3.add_tags: {e}")
                return

        if title:
            audio["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            audio["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            audio["TALB"] = TALB(encoding=3, text=album)
        if track_number:
            audio["TRCK"] = TRCK(encoding=3, text=str(track_number))
        audio.save()
        approaches_tried += 1
    except Exception as e:
        logger.warning(f"⚠️  Metadata: Approach 1 failed: {e}")

    # Approach 2: EasyID3 as fallback (simpler API, more forgiving)
    if approaches_tried == 0:
        try:
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(filepath)
            except ID3Error:
                mp3 = MP3(filepath)
                mp3.add_tags()
                mp3.save()
                audio = EasyID3(filepath)

            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            if album:
                audio["album"] = album
            if track_number:
                audio["tracknumber"] = str(track_number)
            audio.save()
            logger.info(f"✅ Metadata written via EasyID3 fallback: {os.path.basename(filepath)}")
        except Exception as e:
            logger.error(f"⚠️  Metadata: All approaches failed for {os.path.basename(filepath)}: {e}")


def find_studio_version(title, artist):
    """Search YouTube Music songs filter to get the studio version video ID.
    Runs with a timeout to avoid blocking downloads.
    """
    result = [None]

    def _search():
        try:
            query = f"{artist} {title}" if artist else title
            results = safe_ytmusic_call(ytmusic.search, query, filter="songs", limit=5)
            for r in results:
                if r.get("videoId") and r.get("title", "").lower() == title.lower():
                    result[0] = r["videoId"]
                    return
            if results and results[0].get("videoId"):
                result[0] = results[0]["videoId"]
        except Exception:
            pass

    search_thread = threading.Thread(target=_search, daemon=True)
    search_thread.start()
    search_thread.join(timeout=15)  # 15 second timeout

    return result[0]


# ─── Download ─────────────────────────────────────────────────────────────────

def _yt_dlp_download(url, out_path, cookies_path=None):
    """Download using yt-dlp Python API."""
    ydl_opts = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioformat": "mp3",
        "audioquality": "192K",
        "addmetadata": True,
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "embedthumbnail": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    
    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.download([url])


def run_download(video_id, key, title, track_number, thumbnail_url, album, artist):
    with progress_lock:
        progress_data[key] = {"status": "starting", "percent": 3, "title": title}

    # Swap to studio/audio version
    studio_id = find_studio_version(title, artist)
    if studio_id:
        video_id = studio_id

    url = f"https://music.youtube.com/watch?v={video_id}"
    thumb_file = download_thumbnail(thumbnail_url)

    # Use clean title from frontend as the filename
    clean_title = safe_filename(title)
    safe_album  = safe_filename(album) if album else ""

    # Create album subfolder if available
    if safe_album:
        out_folder = os.path.join(DOWNLOAD_FOLDER, safe_album)
        os.makedirs(out_folder, exist_ok=True)
    else:
        out_folder = DOWNLOAD_FOLDER

    out_path = os.path.join(out_folder, f"{clean_title}.%(ext)s")
    cookies_path = os.path.join(BASE_DIR, "cookies.txt")

    try:
        # Setup progress hook
        def progress_hook(d):
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "0%").replace("%", "").strip()
                try:
                    pct_val = float(pct)
                    with progress_lock:
                        progress_data[key]["percent"] = min(int(pct_val), 93)
                        progress_data[key]["status"]  = "downloading"
                except Exception:
                    pass
            elif d["status"] == "finished":
                with progress_lock:
                    progress_data[key]["status"]  = "converting"
                    progress_data[key]["percent"] = 96

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_path,
            "quiet": True,
            "no_warnings": True,
            "embedthumbnail": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "progress_hooks": [progress_hook],
        }
        
        if os.path.exists(cookies_path):
            ydl_opts["cookiefile"] = cookies_path
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Clean up temp thumbnail
        if thumb_file and os.path.exists(thumb_file):
            try:
                os.remove(thumb_file)
            except Exception:
                pass

        # Write metadata with mutagen for reliability
        mp3_path = os.path.join(out_folder, f"{clean_title}.mp3")
        if os.path.exists(mp3_path):
            write_metadata(
                mp3_path,
                title=title,
                artist=artist,
                album=album,
                track_number=track_number,
            )
        with progress_lock:
            progress_data[key] = {
                "status":  "done",
                "percent": 100,
                "title":   clean_title,
                "folder":  out_folder,
                "_ts":     time.time(),
            }
        logger.info(f"✅ Download complete: {clean_title}")

    except Exception as e:
        if thumb_file and os.path.exists(thumb_file):
            try:
                os.remove(thumb_file)
            except Exception:
                pass
        error_msg = str(e)
        if "429" in error_msg or "Sign in" in error_msg or "bot" in error_msg.lower():
            error_msg = "YouTube rate limit or authentication error. Please wait a few minutes or re-run setup_auth.py."
        elif "ffmpeg" in error_msg.lower():
            error_msg = "ffmpeg not found. Please install ffmpeg first: winget install ffmpeg"
        with progress_lock:
            progress_data[key] = {"status": "error", "percent": 0, "error": error_msg, "_ts": time.time()}
        logger.error(f"❌ Download exception: {title} - {error_msg}")


@app.route("/download", methods=["POST"])
def start_download():
    data         = request.get_json()
    video_id     = data.get("videoId", "").strip()
    title        = data.get("title", "Track")
    track_number = data.get("trackNumber")
    thumbnail    = data.get("thumbnail", "")
    album        = data.get("album", "")
    artist       = data.get("artist", "")

    if not video_id:
        return jsonify({"error": "No videoId provided"}), 400

    # Validate video_id
    if not validate_id(video_id):
        return jsonify({"error": "Invalid videoId format"}), 400

    # Use a unique key per download attempt to avoid race conditions
    # when the same song is queued multiple times
    key = f"{video_id}_{int(time.time() * 1000)}"
    with progress_lock:
        progress_data[key] = {"status": "starting", "percent": 0, "title": title, "_ts": time.time()}

    thread = threading.Thread(
        target=run_download,
        args=(video_id, key, title, track_number, thumbnail, album, artist),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "key": key})


@app.route("/progress")
def get_progress():
    key = request.args.get("key", "")
    # Clean up old completed/failed entries (older than 5 minutes)
    _cleanup_progress_data()
    with progress_lock:
        return jsonify(progress_data.get(key, {"status": "unknown", "percent": 0}))


PROGRESS_DATA_TTL = 300  # 5 minutes


def _cleanup_progress_data():
    """Remove completed/failed progress entries older than TTL."""
    now = time.time()
    stale_keys = []
    with progress_lock:
        for k, v in progress_data.items():
            # Add timestamp if not present (for backward compat)
            if "_ts" not in v:
                v["_ts"] = now
                continue
            # Remove if done/error and older than TTL
            if v.get("status") in ("done", "error", "unknown") and (now - v["_ts"]) > PROGRESS_DATA_TTL:
                stale_keys.append(k)
        for k in stale_keys:
            del progress_data[k]


def periodic_progress_cleanup():
    """Background thread for periodic progress data cleanup."""
    while True:
        time.sleep(300)  # Every 5 minutes
        try:
            _cleanup_progress_data()
            logger.debug("Progress data cleanup completed")
        except Exception as e:
            logger.warning(f"Progress data cleanup error: {e}")


# Start periodic cleanup thread
cleanup_thread = threading.Thread(target=periodic_progress_cleanup, daemon=True)
cleanup_thread.start()


@app.route("/folder")
def get_folder():
    return jsonify({"folder": DOWNLOAD_FOLDER})


# ─── Streaming ────────────────────────────────────────────────────────────────

def get_audio_stream_url(video_id):
    """Get the direct audio stream URL from yt-dlp."""
    cookies_path = os.path.join(BASE_DIR, "cookies.txt")
    
    # Try multiple approaches in order of preference
    approaches = [
        # Approach 1: Standard with cookies
        ["--format", "bestaudio/best", "--get-url", "--no-warnings",
         "--js-runtimes", "node", "--cookies", cookies_path],
        # Approach 2: Without cookies but with iOS client
        ["--format", "bestaudio/best", "--get-url", "--no-warnings",
         "--js-runtimes", "node", "--extractor-args", "youtube:player_client=ios"],
        # Approach 3: TV client
        ["--format", "bestaudio/best", "--get-url", "--no-warnings",
         "--extractor-args", "youtube:player_client=tv_embedded"],
    ]
    
    last_error = None
    for extra_args in approaches:
        try:
            cmd = YTDLP_CMD + [f"https://music.youtube.com/watch?v={video_id}"] + extra_args
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                url = result.stdout.strip()
                if url.startswith("http"):
                    return url
            
            # Check if this was a rate limit error
            stderr = result.stderr.lower() if result.stderr else ""
            if "429" in stderr or "sign in" in stderr or "bot" in stderr:
                last_error = "YouTube rate limit detected"
                continue
            elif result.returncode != 0:
                last_error = result.stderr[:200] if result.stderr else "Unknown error"
                continue
                
        except subprocess.TimeoutExpired:
            last_error = "Request timed out"
            continue
        except Exception as e:
            last_error = str(e)
            continue
    
    # All approaches failed
    if last_error:
        if "429" in last_error.lower() or "rate limit" in last_error.lower():
            raise Exception("YouTube is rate-limiting requests from your IP. Please wait 30-60 minutes.")
        elif "sign in" in last_error.lower() or "bot" in last_error.lower():
            raise Exception("YouTube authentication failed. Please re-run authentication setup.")
        else:
            raise Exception(f"Could not get stream URL: {last_error[:200]}")
    
    return None


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    """Proxy audio stream from YouTube Music with reconnection support."""
    global now_playing

    # Validate video_id
    if not validate_id(video_id):
        return jsonify({"error": "Invalid video ID format"}), 400

    stream_url = get_audio_stream_url(video_id)
    if not stream_url:
        return jsonify({"error": "Could not get stream URL"}), 500

    # Store now playing info (thread-safe)
    with progress_lock:
        now_playing.update({
            "videoId": video_id,
            "stream_url": stream_url,
        })

    # Stream the audio content with reconnection logic
    try:
        def generate():
            max_retries = 5
            retry_count = 0
            byte_offset = 0

            while retry_count < max_retries:
                try:
                    headers = {}
                    if byte_offset > 0:
                        headers["Range"] = f"bytes={byte_offset}-"

                    with req.get(
                        stream_url,
                        stream=True,
                        timeout=120,
                        headers=headers,
                    ) as r:
                        # If server honors range request, we got 206 Partial Content
                        # If not, we restart from the beginning
                        if r.status_code not in (200, 206):
                            r.raise_for_status()

                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                byte_offset += len(chunk)
                                yield chunk
                                retry_count = 0  # Reset retry counter on successful data

                        # If we exit the loop cleanly, the stream is complete
                        break

                except (req.exceptions.ConnectionError, req.exceptions.ReadTimeout):
                    retry_count += 1
                    if retry_count >= max_retries:
                        print(f"Stream reconnect failed after {max_retries} retries for {video_id}")
                        break
                    print(f"Stream reconnect attempt {retry_count}/{max_retries} for {video_id}")
                    time.sleep(1 * retry_count)  # Exponential backoff

        # Get content type from response
        with req.head(stream_url, timeout=15) as r:
            content_type = r.headers.get("Content-Type", "audio/webm")

        return Response(
            stream_with_context(generate()),
            mimetype=content_type,
            headers={
                "Cache-Control": "no-cache",
                "Accept-Ranges": "bytes",
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/now_playing")
def get_now_playing():
    """Get current now playing info."""
    return jsonify(now_playing)


@app.route("/api/watch/playlist")
def watch_playlist():
    """Get watch playlist (related songs) for a given video ID.
    This is what YouTube Music uses for auto-generated queues/radio mode.
    """
    video_id = request.args.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "No videoId provided"}), 400
    
    try:
        # Get watch playlist - returns related songs for radio mode
        data = safe_ytmusic_call(ytmusic.get_watch_playlist, video_id, limit=25)
        
        tracks = []
        for t in data.get("tracks", []):
            if t.get("videoId"):
                tracks.append(format_song(t))
        
        return jsonify({
            "tracks": tracks,
            "lyrics": data.get("lyrics"),  # May include lyrics ID
        })
    except Exception as e:
        print(f"Watch playlist error: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Playlist Management ──────────────────────────────────────────────────────

@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    """Get all user playlists."""
    try:
        data = safe_ytmusic_call(ytmusic.get_library_playlists, limit=100)
        playlists = []
        for p in data:
            playlists.append({
                "playlistId": p.get("playlistId", ""),
                "title": p.get("title", ""),
                "count": p.get("count", 0),
                "thumbnail": get_thumb(p.get("thumbnails")),
            })
        return jsonify({"playlists": playlists})
    except Exception as e:
        print(f"Get playlists error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/<playlist_id>", methods=["GET"])
def get_playlist(playlist_id):
    """Get playlist details and tracks."""
    try:
        data = safe_ytmusic_call(ytmusic.get_playlist, playlist_id, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({
            "playlistId": playlist_id,
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "author": data.get("author", {}).get("name", "") if data.get("author") else "",
            "count": data.get("trackCount", len(tracks)),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "tracks": tracks,
        })
    except Exception as e:
        print(f"Get playlist error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/create", methods=["POST"])
def create_playlist():
    """Create a new playlist."""
    data = request.get_json()
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    privacy = data.get("privacy", "PRIVATE")  # PUBLIC, PRIVATE, UNLISTED
    
    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    try:
        playlist_id = safe_ytmusic_call(
            ytmusic.create_playlist,
            title=title,
            description=description,
            privacy_status=privacy.upper()
        )
        return jsonify({
            "success": True,
            "playlistId": playlist_id,
            "title": title,
        })
    except Exception as e:
        print(f"Create playlist error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/<playlist_id>/delete", methods=["POST"])
def delete_playlist(playlist_id):
    """Delete a playlist."""
    try:
        result = safe_ytmusic_call(ytmusic.delete_playlist, playlist_id)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        print(f"Delete playlist error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/<playlist_id>/add", methods=["POST"])
def add_to_playlist(playlist_id):
    """Add songs to a playlist."""
    data = request.get_json()
    video_ids = data.get("videoIds", [])
    
    if not video_ids:
        return jsonify({"error": "No videoIds provided"}), 400
    
    try:
        result = safe_ytmusic_call(
            ytmusic.add_playlist_items,
            playlistId=playlist_id,
            videoIds=video_ids,
            duplicates=False
        )
        return jsonify({"success": True, "result": result})
    except Exception as e:
        print(f"Add to playlist error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/<playlist_id>/remove", methods=["POST"])
def remove_from_playlist(playlist_id):
    """Remove songs from a playlist."""
    data = request.get_json()
    videos = data.get("videos", [])  # List of {videoId, setVideoId}
    
    if not videos:
        return jsonify({"error": "No videos provided"}), 400
    
    try:
        result = safe_ytmusic_call(ytmusic.remove_playlist_items, playlistId=playlist_id, videos=videos)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        print(f"Remove from playlist error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/song/rate", methods=["POST"])
def rate_song():
    """Like or unlike a song."""
    data = request.get_json()
    video_id = data.get("videoId", "").strip()
    rating = data.get("rating", "INDIFFERENT")  # LIKE, DISLIKE, INDIFFERENT

    if not video_id:
        return jsonify({"error": "No videoId provided"}), 400

    try:
        rating_map = {
            "LIKE": LikeStatus.LIKE,
            "DISLIKE": LikeStatus.DISLIKE,
            "INDIFFERENT": LikeStatus.INDIFFERENT,
        }
        like_status = rating_map.get(rating.upper(), LikeStatus.INDIFFERENT)
        result = safe_ytmusic_call(ytmusic.rate_song, videoId=video_id, rating=like_status)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        print(f"Rate song error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/song/rating", methods=["GET"])
def get_song_rating():
    """Get the current rating (like status) of a song."""
    video_id = request.args.get("videoId", "").strip()
    
    if not video_id:
        return jsonify({"error": "No videoId provided"}), 400
    
    try:
        # Get the song info from watch playlist which includes like status
        data = safe_ytmusic_call(ytmusic.get_watch_playlist, video_id, limit=1)
        if data.get("tracks"):
            track = data["tracks"][0]
            like_status = track.get("likeStatus", "INDIFFERENT")
            return jsonify({"videoId": video_id, "likeStatus": like_status})
        return jsonify({"videoId": video_id, "likeStatus": "INDIFFERENT"})
    except Exception as e:
        print(f"Get song rating error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/liked", methods=["GET"])
def get_liked_songs():
    """Get liked songs from library."""
    try:
        data = safe_ytmusic_call(ytmusic.get_liked_songs, limit=100)
        tracks = [format_song(t) for t in data.get("tracks", []) if t.get("videoId")]
        return jsonify({
            "playlistId": data.get("id", "LM"),
            "title": data.get("name", "Liked Songs"),
            "count": data.get("trackCount", len(tracks)),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "tracks": tracks,
        })
    except Exception as e:
        print(f"Get liked songs error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    # Check if user is authenticated
    browser_json = os.path.join(BASE_DIR, "browser.json")
    if not os.path.exists(browser_json):
        return send_from_directory("static", "setup.html")
    
    # Verify auth works
    try:
        from ytmusicapi import YTMusic
        ytmusic_test = YTMusic(browser_json)
        ytmusic_test.get_home(limit=1)
        return send_from_directory("static", "index.html")
    except Exception:
        # Auth failed, show setup page
        return send_from_directory("static", "setup.html")


@app.route("/setup")
def setup_page():
    return send_from_directory("static", "setup.html")


# ─── Format helpers ───────────────────────────────────────────────────────────

def format_song(s):
    artists = s.get("artists") or []
    return {
        "videoId":   s.get("videoId", ""),
        "title":     s.get("title", ""),
        "artist":    ", ".join(a.get("name", "") for a in artists),
        "album":     s.get("album", {}).get("name", "") if s.get("album") else "",
        "duration":  s.get("duration", ""),
        "thumbnail": get_thumb(s.get("thumbnails")),
    }


def format_artist(a):
    return {
        "browseId":    a.get("browseId", ""),
        "name":        a.get("artist", "") or a.get("name", "") or a.get("title", ""),
        "subscribers": a.get("subscribers", ""),
        "thumbnail":   get_thumb(a.get("thumbnails")),
    }


def format_album(a):
    artists = a.get("artists") or []
    return {
        "browseId":  a.get("browseId", ""),
        "title":     a.get("title", ""),
        "artist":    ", ".join(x.get("name", "") for x in artists),
        "year":      a.get("year", ""),
        "thumbnail": get_thumb(a.get("thumbnails")),
    }


# ─── Auto-install ffmpeg if missing ──────────────────────────────────────────

if not shutil.which("ffmpeg"):
    logger.info("⏳ Installing ffmpeg... (one-time setup)")
    try:
        subprocess.run(
            ["winget", "install", "ffmpeg", "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if shutil.which("ffmpeg"):
            logger.info("✅ ffmpeg installed successfully!")
        else:
            logger.warning("⚠️  ffmpeg auto-install failed. Please install manually: winget install ffmpeg")
    except Exception as e:
        logger.warning(f"⚠️  ffmpeg auto-install failed: {e}")
else:
    logger.info("✅ ffmpeg found")


if __name__ == "__main__":
    logger.info(f"✅ DECIBEL running at http://0.0.0.0:5000")
    logger.info(f"📁 Saving to: {DOWNLOAD_FOLDER}")
    app.run(host="0.0.0.0", debug=False, port=5000)


# ─── Security Headers ─────────────────────────────────────────────────────────

def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    
    # Only disable caching for API endpoints, not static assets
    if request.path.startswith('/api') or request.path.startswith('/setup'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    
    return response

app.after_request(add_security_headers)

