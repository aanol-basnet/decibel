import os
import re
import threading
import subprocess
import tempfile
import json
import secrets
import requests as req
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, session, redirect, url_for
from flask_cors import CORS
from ytmusicapi import YTMusic, LikeStatus
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, error as ID3Error
from mutagen.mp3 import MP3
from google_auth_oauthlib.flow import Flow
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

app = Flask(__name__, static_folder="static")
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True)

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Music", "Downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# OAuth configuration
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

# Works on Windows, Linux, and macOS
YTDLP = "yt-dlp"

# Current playing track info (for streaming state)
now_playing = {}


# ─── OAuth 2.0 Authentication ─────────────────────────────────────────────────

def get_oauth_flow():
    """Create and return OAuth flow object."""
    if not os.path.exists(CLIENT_CONFIG_FILE):
        return None
    with open(CLIENT_CONFIG_FILE, "r") as f:
        client_config = json.load(f)
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri="http://localhost:5000/oauth/callback"
    )
    return flow


def initialize_ytmusic_with_token():
    """Initialize YTMusic with OAuth token if available."""
    global ytmusic
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                token_info = json.load(f)
            ytmusic = YTMusic(headers_auth=token_info)
            print("✅ YTMusic initialized with OAuth token")
            return True
        except Exception as e:
            print(f"⚠️  Failed to initialize YTMusic with token: {e}")
    # Fall back to browser.json if exists
    browser_file = os.path.join(BASE_DIR, "browser.json")
    if os.path.exists(browser_file):
        try:
            ytmusic = YTMusic(browser_file)
            print("✅ YTMusic initialized with browser.json")
            return True
        except Exception as e:
            print(f"⚠️  Failed to initialize YTMusic with browser.json: {e}")
    # Unauthenticated
    ytmusic = YTMusic()
    print("ℹ️  YTMusic running in unauthenticated mode")
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
        
        return redirect("/?login=success")
    except Exception as e:
        return redirect(f"/?login=error&message={str(e)}")


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
            ytmusic = YTMusic(browser_file)
        else:
            ytmusic = YTMusic()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/oauth/status")
def oauth_status():
    """Get current OAuth login status."""
    user_info = get_user_info()
    return jsonify(user_info)


# Initialize YTMusic on startup
initialize_ytmusic_with_token()


# ─── Home / Library ───────────────────────────────────────────────────────────

@app.route("/home")
def home():
    try:
        data     = ytmusic.get_home(limit=6)
        sections = []
        for section in data:
            title   = section.get("title", "")
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


# Cache all library artists on first load
_library_artists_cache = None

@app.route("/library/artists")
def library_artists():
    global _library_artists_cache
    page   = int(request.args.get("page", 0))
    size   = 80
    try:
        if _library_artists_cache is None:
            data = ytmusic.get_library_artists(limit=500)
            print("Library artists fetched:", len(data))
            _library_artists_cache = [format_artist(a) for a in data]
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
        data = ytmusic.get_library_playlists(limit=50)
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
    try:
        data   = ytmusic.get_playlist(playlist_id, limit=100)
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
        songs   = ytmusic.search(query, filter="songs",   limit=8)
        artists = ytmusic.search(query, filter="artists", limit=8)
        albums  = ytmusic.search(query, filter="albums",  limit=4)
        return jsonify({
            "songs":   [format_song(s)   for s in songs],
            "artists": [format_artist(a) for a in artists],
            "albums":  [format_album(a)  for a in albums],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/artist/<browse_id>")
def artist_page(browse_id):
    try:
        data   = ytmusic.get_artist(browse_id)
        songs  = [format_song(s) for s in (data.get("songs", {}).get("results") or [])[:10]]
        albums_preview = (data.get("albums", {}).get("results") or [])
        albums_params  = data.get("albums", {}).get("params")
        albums_id      = data.get("albums", {}).get("browseId")
        if albums_id and albums_params:
            try:
                full   = ytmusic.get_artist_albums(albums_id, albums_params)
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
    try:
        data    = ytmusic.get_album(browse_id)
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
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


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
    """Write ID3 metadata directly into MP3 using mutagen."""
    try:
        try:
            audio = ID3(filepath)
        except ID3Error:
            mp3 = MP3(filepath)
            mp3.add_tags()
            audio = ID3(filepath)
        if title:
            audio["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            audio["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            audio["TALB"] = TALB(encoding=3, text=album)
        if track_number:
            audio["TRCK"] = TRCK(encoding=3, text=str(track_number))
        audio.save()
    except Exception:
        pass


def find_studio_version(title, artist):
    """Search YouTube Music songs filter to get the studio version video ID."""
    try:
        query   = f"{artist} {title}" if artist else title
        results = ytmusic.search(query, filter="songs", limit=5)
        for r in results:
            if r.get("videoId") and r.get("title", "").lower() == title.lower():
                return r["videoId"]
        if results and results[0].get("videoId"):
            return results[0]["videoId"]
    except Exception:
        pass
    return None


# ─── Download ─────────────────────────────────────────────────────────────────

def run_download(video_id, key, title, track_number, thumbnail_url, album, artist):
    progress_data[key] = {"status": "starting", "percent": 3, "title": title}

    # Swap to studio/audio version
    studio_id = find_studio_version(title, artist)
    if studio_id:
        video_id = studio_id

    url        = f"https://music.youtube.com/watch?v={video_id}"
    log_path   = os.path.join(DOWNLOAD_FOLDER, "download.log")
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

    cmd = [
        YTDLP,
        url,
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "--add-metadata",
        "--output", out_path,
        "--newline",
        "--no-warnings",
    ]

    # Embed album art — on Windows avoid the custom ffmpeg_i ppa as it can cause issues
    # Just use yt-dlp's built-in embed which grabs YouTube's thumbnail
    cmd += ["--embed-thumbnail"]

    try:
        with open(log_path, "w") as log:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                bufsize=1,
                # Needed on Windows to avoid console window popup
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "[download]" in line and "%" in line:
                try:
                    pct = int(float(line.split("%")[0].split()[-1]))
                    progress_data[key]["percent"] = min(pct, 93)
                    progress_data[key]["status"]  = "downloading"
                except Exception:
                    pass
            elif "[ExtractAudio]" in line or "Converting" in line:
                progress_data[key]["status"]  = "converting"
                progress_data[key]["percent"] = 96

        process.wait()

        # Clean up temp thumbnail
        if thumb_file and os.path.exists(thumb_file):
            try:
                os.remove(thumb_file)
            except Exception:
                pass

        if process.returncode == 0:
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
            progress_data[key] = {
                "status":  "done",
                "percent": 100,
                "title":   clean_title,
                "folder":  out_folder,
            }
        else:
            progress_data[key] = {
                "status":  "error",
                "percent": 0,
                "error":   "Download failed. Check download.log in your Music/Downloads folder.",
            }

    except Exception as e:
        if thumb_file and os.path.exists(thumb_file):
            try:
                os.remove(thumb_file)
            except Exception:
                pass
        progress_data[key] = {"status": "error", "percent": 0, "error": str(e)}


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

    key = video_id
    progress_data[key] = {"status": "starting", "percent": 0, "title": title}

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
    return jsonify(progress_data.get(key, {"status": "unknown", "percent": 0}))


@app.route("/folder")
def get_folder():
    return jsonify({"folder": DOWNLOAD_FOLDER})


# ─── Streaming ────────────────────────────────────────────────────────────────

def get_audio_stream_url(video_id):
    """Get the direct audio stream URL from yt-dlp."""
    try:
        cmd = [
            YTDLP,
            f"https://music.youtube.com/watch?v={video_id}",
            "--format", "bestaudio/best",
            "--get-url",
            "--no-warnings",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"Error getting stream URL: {e}")
    return None


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    """Proxy audio stream from YouTube Music."""
    global now_playing
    
    stream_url = get_audio_stream_url(video_id)
    if not stream_url:
        return jsonify({"error": "Could not get stream URL"}), 500
    
    # Store now playing info
    now_playing = {
        "videoId": video_id,
        "stream_url": stream_url,
    }
    
    # Stream the audio content
    try:
        def generate():
            with req.get(stream_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
        
        # Get content type from response
        with req.head(stream_url, timeout=10) as r:
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
        data = ytmusic.get_watch_playlist(video_id, limit=25)
        
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
        data = ytmusic.get_library_playlists(limit=100)
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
        data = ytmusic.get_playlist(playlist_id, limit=100)
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
        playlist_id = ytmusic.create_playlist(
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
        result = ytmusic.delete_playlist(playlist_id)
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
        result = ytmusic.add_playlist_items(
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
        result = ytmusic.remove_playlist_items(playlistId=playlist_id, videos=videos)
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
        result = ytmusic.rate_song(videoId=video_id, rating=like_status)
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
        data = ytmusic.get_watch_playlist(video_id, limit=1)
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
        data = ytmusic.get_liked_songs(limit=100)
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
    return send_from_directory("static", "index.html")


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
        "name":        a.get("artist", "") or a.get("name", ""),
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


if __name__ == "__main__":
    print(f"✅ DECIBEL running at http://0.0.0.0:5000")
    print(f"📁 Saving to: {DOWNLOAD_FOLDER}")
    app.run(host="0.0.0.0", debug=False, port=5000)