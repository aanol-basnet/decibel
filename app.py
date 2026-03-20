import os
import re
import threading
import subprocess
import tempfile
import requests as req
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from ytmusicapi import YTMusic
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, error as ID3Error
from mutagen.mp3 import MP3

app = Flask(__name__, static_folder="static")
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Music", "Downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

_auth = os.path.join(os.path.dirname(__file__), "browser.json")
ytmusic = YTMusic(_auth) if os.path.exists(_auth) else YTMusic()
progress_data = {}

# Works on Windows, Linux, and macOS
YTDLP = "yt-dlp"


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
    size   = 50
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