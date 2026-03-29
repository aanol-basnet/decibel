# DECIBEL — Music Downloader

A minimal, self-hosted music downloader with a browser-based UI. Browse and download music from YouTube Music as MP3s — with full metadata, album art, and per-album folders. Optionally connect your YouTube Music account to get personalized recommendations, your subscribed artists, and your playlists.

---

## Features

- Search artists, albums, and songs via YouTube Music
- Browse artist pages and full album tracklists
- Top results surfaced based on your search query
- Recently searched artists shown on the home page
- Download individual songs or entire albums at once
- Download queue — handles multiple downloads one at a time
- Automatically finds the studio version of each track
- Saves as MP3 at 192kbps with embedded album art
- Full metadata — title, artist, album, track number written via mutagen
- Organizes downloads into per-album folders
- Connect your YouTube Music account to unlock:
  - Personalized home recommendations
  - Your subscribed artists (with infinite scroll)
  - Your playlists

---

## Requirements

- Python 3.9+
- ffmpeg

---

## Setup

### 1. Install ffmpeg

**Ubuntu / Debian**
```bash
sudo apt install ffmpeg -y
```

**macOS**
```bash
brew install ffmpeg
```

**Windows**
```bash
winget install ffmpeg
```

---

### 2. Clone the repo

```bash
git clone https://github.com/aanol-basnet/decibel.git
cd decibel
```

---

### 3. Install Python dependencies

**Ubuntu / Debian / macOS**
```bash
pip install yt-dlp flask flask-cors ytmusicapi mutagen requests browser_cookie3 --break-system-packages
```

**Windows** (run inside your virtual environment)
```bash
pip install yt-dlp flask flask-cors ytmusicapi mutagen requests browser_cookie3
```

---

### 4. Fix PATH (Linux / macOS only)

If you get a `yt-dlp: command not found` error, run:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

---

## Running

**Linux / macOS**
```bash
python3 app.py
```

**Windows**
```bash
python app.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Connecting your YouTube Music account (optional)

This unlocks personalized recommendations, your subscribed artists, and your playlists.

### Automatic Setup (Recommended)

One command does everything — no manual copy-pasting required!

1. Make sure you're logged into [music.youtube.com](https://music.youtube.com) in your browser
2. Run the setup script:
   ```bash
   python setup_auth.py
   ```
3. Choose your browser (or press Enter for auto-detect)
4. The script automatically extracts authentication and creates both files
5. Add these to `.gitignore`:
   ```bash
   echo "browser.json" >> .gitignore
   echo "cookies.txt" >> .gitignore
   ```

That's it! The script will:
- ✅ Extract authentication cookies from your browser
- ✅ Generate the required Authorization header automatically
- ✅ Create `browser.json` for ytmusicapi
- ✅ Create `cookies.txt` for yt-dlp
- ✅ Verify everything works

### Manual Setup (if automatic fails)

If the automatic setup doesn't work:

1. Create `browser.json` manually:
   ```bash
   ytmusicapi browser
   ```
2. Follow the prompts to copy headers from browser DevTools
3. Create `cookies.txt` using a browser extension like "Get cookies.txt LOCALLY"

The app automatically uses `browser.json` if it exists. If not, it falls back to unauthenticated mode.

---

## Accessing from another device on the same network

The app runs on `0.0.0.0` by default, so any device on your local network can access it.

Find your machine's local IP:

- **Linux / macOS**: run `ifconfig` and look for something like `192.168.x.x`
- **Windows**: run `ipconfig` and look for `IPv4 Address`

Then on the other device, open:
```
http://<your-ip>:5000
```

---

## Downloads

Files are saved to per-album folders inside:

- **Linux / macOS**: `~/Music/Downloads/<Album Name>/`
- **Windows**: `C:\Users\<you>\Music\Downloads\<Album Name>\`

---

## Project Structure

```
decibel/
├── app.py            # Flask backend
├── setup_auth.py     # Automatic cookie extractor (run this first!)
├── README.md
├── browser.json      # YouTube Music auth (optional, not committed)
├── cookies.txt       # yt-dlp cookies (optional, not committed)
└── static/
    └── index.html    # Frontend UI
```

---

## .gitignore

Make sure your `.gitignore` includes:

```
browser.json
cookies.txt
*.mp3
*.log
__pycache__/
*.pyc
venv/
```

---

## Troubleshooting

**Rate limited by YouTube?**
Wait 24 hours or export your browser cookies using the "Get cookies.txt LOCALLY" extension, save as `cookies.txt` in the project folder, then add `--cookie-file cookies.txt` to the yt-dlp command in `app.py`.

**`yt-dlp` not found?**
Make sure `~/.local/bin` is on your PATH — see step 4 above.

**`ffmpeg` not found?**
Make sure ffmpeg is installed and on your PATH. Test with `ffmpeg -version`.

**Songs not sorting by track number on my phone?**
Use the "Download All" button on an album page — this ensures correct track numbers are written to every file's metadata.

**Windows: "system cannot find the file specified"?**
Make sure you're running `python app.py` from inside the virtual environment (`source venv/Scripts/activate` in Git Bash, or `venv\Scripts\activate` in PowerShell).

**YouTube Music auth not working?**
Re-run `ytmusicapi browser` and paste fresh headers — your previous headers may have expired.