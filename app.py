from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK
from mutagen.mp3 import MP3
import os
import re
import threading
import subprocess
import tempfile
import requests as req
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from ytmusicapi import YTMusic

app = Flask(__name__, static_folder="static")
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Music", "Downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

ytmusic = YTMusic()
progress_data = {}

YTDLP = os.path.expanduser("~/.local/bin/yt-dlp")
if not os.path.exists(YTDLP):
    YTDLP = "yt-dlp"


# ─── Search ───────────────────────────────────────────────────────────────────

@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    try:
        songs   = ytmusic.search(query, filter="songs",   limit=8)
        artists = ytmusic.search(query, filter="artists", limit=4)
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
        data = ytmusic.get_artist(browse_id)
        songs  = [format_song(s)  for s in (data.get("songs",  {}).get("results") or [])[:10]]
        albums = [format_album(a) for a in (data.get("albums", {}).get("results") or [])[:10]]
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


# ─── Download ─────────────────────────────────────────────────────────────────

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


def clean_title(title):
    """Strip 'Artist - ' prefix and ' (Audio)' suffix from title."""
    title = re.sub(r"^.+ - ", "", title)
    title = re.sub(r"\s*\(Audio\)\s*$", "", title, flags=re.IGNORECASE)
    return title.strip()

def write_metadata(filepath, title=None, artist=None, album=None, track_number=None):
    """Write metadata directly into MP3 using mutagen."""
    try:
        audio = ID3(filepath)
    except Exception:
        try:
            audio = MP3(filepath)
            audio.add_tags()
            audio = ID3(filepath)
        except Exception:
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


def find_studio_version(title, artist):
    """Search YouTube Music for the studio/audio version and return its video ID."""
    try:
        query = f"{artist} {title}" if artist else title
        results = ytmusic.search(query, filter="songs", limit=5)
        for r in results:
            if r.get("videoId") and r.get("title", "").lower() == title.lower():
                return r["videoId"]
        if results and results[0].get("videoId"):
            return results[0]["videoId"]
    except Exception:
        pass
    return None

def run_download(video_id, key, title, track_number, thumbnail_url, album, artist):
    progress_data[key] = {"status": "starting", "percent": 3, "title": title}
    # Try to swap to studio/audio version
    studio_id = find_studio_version(title, artist)
    if studio_id:
        video_id = studio_id
    url = f"https://music.youtube.com/watch?v={video_id}"

    log_path = os.path.join(DOWNLOAD_FOLDER, "download.log")
    thumb_file = download_thumbnail(thumbnail_url)

    # Output folder — per album if available, else root
    safe_album = re.sub(r'[<>:"/\\|?*]', "", album) if album else ""
    if safe_album:
        out_folder = os.path.join(DOWNLOAD_FOLDER, safe_album)
        os.makedirs(out_folder, exist_ok=True)
    else:
        out_folder = DOWNLOAD_FOLDER

    cmd = [
        YTDLP,
        url,
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "--add-metadata",
        "--replace-in-metadata", "title", r"^.+ - ", "",
        "--replace-in-metadata", "title", r"(?i)\s*\(Audio\)\s*$", "",
        "--output", os.path.join(out_folder, "%(title)s.%(ext)s"),
        "--newline",
        "--no-warnings",
    ]

    # Embed custom album art if we downloaded it, otherwise use YouTube's
    if thumb_file:
        cmd += [
            "--embed-thumbnail",
            "--convert-thumbnail", "jpg",
            "--ppa", f"EmbedThumbnail+ffmpeg_i:-i {thumb_file}",
        ]
    else:
        cmd += ["--embed-thumbnail"]

    # Inject metadata via ffmpeg postprocessor
    if track_number:
        cmd += ["--ppa", f"FFmpegMetadata+ffmpeg_o:-metadata track={track_number}"]
    if album:
        cmd += ["--ppa", f"FFmpegMetadata+ffmpeg_o:-metadata album={album}"]
    if artist:
        cmd += ["--ppa", f"FFmpegMetadata+ffmpeg_o:-metadata artist={artist}"]

    try:
        with open(log_path, "w") as log:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                bufsize=1,
            )

        final_title = title
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "[download]" in line and "%" in line:
                try:
                    pct = int(float(line.split("%")[0].split()[-1]))
                    progress_data[key]["percent"] = min(pct, 93)
                    progress_data[key]["status"] = "downloading"
                except Exception:
                    pass
            elif "[ExtractAudio]" in line or "Converting" in line:
                progress_data[key]["status"] = "converting"
                progress_data[key]["percent"] = 96
            elif "Destination:" in line:
                try:
                    raw = line.split("Destination:")[-1].strip().rsplit(".", 1)[0]
                    final_title = os.path.basename(raw)
                    progress_data[key]["title"] = final_title
                except Exception:
                    pass

        process.wait()

        # Clean up temp thumbnail
        if thumb_file and os.path.exists(thumb_file):
            os.remove(thumb_file)

        if process.returncode == 0:
            # Write metadata with mutagen for reliability
            mp3_path = os.path.join(out_folder, final_title + ".mp3")
            if os.path.exists(mp3_path):
                write_metadata(mp3_path, title=final_title, artist=artist, album=album, track_number=track_number)
            progress_data[key] = {
                "status":  "done",
                "percent": 100,
                "title":   final_title,
                "folder":  out_folder,
            }
        else:
            progress_data[key] = {
                "status": "error",
                "percent": 0,
                "error": "Download failed. Check download.log in your music folder.",
            }

    except Exception as e:
        if thumb_file and os.path.exists(thumb_file):
            os.remove(thumb_file)
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_thumb(thumbnails):
    if not thumbnails:
        return ""
    return thumbnails[-1].get("url", "")


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