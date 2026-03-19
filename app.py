import os
import threading
import subprocess
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
        songs = ytmusic.search(query, filter="songs", limit=8)
        artists = ytmusic.search(query, filter="artists", limit=4)
        albums = ytmusic.search(query, filter="albums", limit=4)

        return jsonify({
            "songs": [format_song(s) for s in songs],
            "artists": [format_artist(a) for a in artists],
            "albums": [format_album(a) for a in albums],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/artist/<browse_id>")
def artist_page(browse_id):
    try:
        data = ytmusic.get_artist(browse_id)
        songs = []
        albums = []

        if data.get("songs") and data["songs"].get("results"):
            songs = [format_song(s) for s in data["songs"]["results"][:10]]

        if data.get("albums") and data["albums"].get("results"):
            albums = [format_album(a) for a in data["albums"]["results"][:10]]

        return jsonify({
            "name": data.get("name", ""),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "subscribers": data.get("subscribers", ""),
            "songs": songs,
            "albums": albums,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/album/<browse_id>")
def album_page(browse_id):
    try:
        data = ytmusic.get_album(browse_id)
        tracks = []
        for t in data.get("tracks", []):
            vid = t.get("videoId")
            tracks.append({
                "videoId": vid,
                "title": t.get("title", ""),
                "duration": t.get("duration", ""),
                "trackNumber": t.get("trackNumber"),
            })

        return jsonify({
            "title": data.get("title", ""),
            "artist": data.get("artists", [{}])[0].get("name", "") if data.get("artists") else "",
            "year": data.get("year", ""),
            "thumbnail": get_thumb(data.get("thumbnails")),
            "tracks": tracks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Download ─────────────────────────────────────────────────────────────────

def run_download(video_id, key):
    url = f"https://music.youtube.com/watch?v={video_id}"
    progress_data[key] = {"status": "downloading", "percent": 5}

    log_path = os.path.join(DOWNLOAD_FOLDER, "download.log")

    cmd = [
        YTDLP,
        url,
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "--embed-thumbnail",
        "--add-metadata",
        "--output", os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s"),
        "--newline",
        "--no-warnings",
    ]

    try:
        with open(log_path, "w") as log:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                bufsize=1,
            )

        title = None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "[download]" in line and "%" in line:
                try:
                    pct = int(float(line.split("%")[0].split()[-1]))
                    progress_data[key]["percent"] = min(pct, 95)
                    progress_data[key]["status"] = "downloading"
                except:
                    pass
            elif "[ExtractAudio]" in line or "Converting" in line:
                progress_data[key] = {"status": "converting", "percent": 97}
            elif "Destination" in line:
                try:
                    title = line.split("Destination:")[-1].strip().rsplit(".", 1)[0]
                    progress_data[key]["title"] = title
                except:
                    pass

        process.wait()

        if process.returncode == 0:
            progress_data[key] = {
                "status": "done",
                "percent": 100,
                "title": title or "Track",
                "folder": DOWNLOAD_FOLDER,
            }
        else:
            progress_data[key] = {
                "status": "error",
                "percent": 0,
                "error": "Download failed. Check ~/Music/Downloads/download.log",
            }
    except Exception as e:
        progress_data[key] = {"status": "error", "percent": 0, "error": str(e)}


@app.route("/download", methods=["POST"])
def start_download():
    data = request.get_json()
    video_id = data.get("videoId", "").strip()
    title = data.get("title", "Track")

    if not video_id:
        return jsonify({"error": "No videoId provided"}), 400

    key = video_id
    progress_data[key] = {"status": "starting", "percent": 0, "title": title}

    thread = threading.Thread(target=run_download, args=(video_id, key), daemon=True)
    thread.start()

    return jsonify({"status": "started", "key": key})


@app.route("/progress")
def get_progress():
    key = request.args.get("key", "")
    return jsonify(progress_data.get(key, {"status": "unknown", "percent": 0}))


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
        "videoId": s.get("videoId", ""),
        "title": s.get("title", ""),
        "artist": ", ".join(a.get("name", "") for a in artists),
        "album": s.get("album", {}).get("name", "") if s.get("album") else "",
        "duration": s.get("duration", ""),
        "thumbnail": get_thumb(s.get("thumbnails")),
    }


def format_artist(a):
    return {
        "browseId": a.get("browseId", ""),
        "name": a.get("artist", "") or a.get("name", ""),
        "subscribers": a.get("subscribers", ""),
        "thumbnail": get_thumb(a.get("thumbnails")),
    }


def format_album(a):
    artists = a.get("artists") or []
    return {
        "browseId": a.get("browseId", ""),
        "title": a.get("title", ""),
        "artist": ", ".join(x.get("name", "") for x in artists),
        "year": a.get("year", ""),
        "thumbnail": get_thumb(a.get("thumbnails")),
    }


if __name__ == "__main__":
    print(f"✅ Music Downloader running at http://0.0.0.0:5000")
    print(f"📁 Saving to: {DOWNLOAD_FOLDER}")
    app.run(host="0.0.0.0", debug=False, port=5000)