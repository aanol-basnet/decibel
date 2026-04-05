# DECIBEL — Music Downloader

A standalone desktop app for streaming and downloading music from YouTube Music as MP3s — with full metadata, album art, per-album folders, and a beautiful native UI.

---

## Features

- **Native desktop window** — no browser needed
- **Zero setup** — ffmpeg auto-installs on first run
- **Easy authentication** — select your browser, click once
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
- Personalized home recommendations
- Your subscribed artists (with infinite scroll)
- Your playlists

---

## For Users — Just Run It

### Requirements

- **Windows 10/11**
- **Logged into [music.youtube.com](https://music.youtube.com) in your browser**

### Setup

1. Run `DECIBEL.exe`
2. ffmpeg will auto-install on first run (takes ~10 seconds)
3. Select your browser (Chrome, Edge, Firefox, Brave, or Opera)
4. Click **Authenticate**
5. That's it — enjoy!

### Downloads

Files are saved to per-album folders inside:

- `C:\Users\<you>\Music\Downloads\<Album Name>\`

---

## For Developers — Build From Source

### Requirements

- Python 3.12+
- ffmpeg (auto-installs if missing)

### Clone & Install

```bash
git clone https://github.com/aanol-basnet/decibel.git
cd decibel
pip install -r requirements.txt
```

### Run

```bash
python app.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

### Build as Desktop App

```bash
pip install pyinstaller
python -m PyInstaller decibel.spec --noconfirm
```

The `.exe` will be in `dist/DECIBEL.exe`.

---

## Authentication

DECIBEL uses browser cookie extraction — no API keys or OAuth needed.

### How It Works

1. You log into music.youtube.com in your browser
2. DECIBEL extracts cookies automatically
3. Creates `browser.json` (for ytmusicapi) and `cookies.txt` (for yt-dlp)
4. Auth is verified before the app starts

### Supported Browsers

- Google Chrome
- Microsoft Edge
- Mozilla Firefox
- Brave
- Opera

### Re-authenticate

If cookies expire (~2 weeks), just delete `browser.json` and `cookies.txt` next to `DECIBEL.exe` and re-run the app.

---

## Project Structure

```
decibel/
├── app.py            # Flask backend
├── launcher.py       # Desktop app entry point (pywebview)
├── setup_auth.py     # Standalone cookie extractor
├── decibel.spec      # PyInstaller build config
├── requirements.txt  # Python dependencies
├── static/
│   ├── index.html    # Main UI
│   └── setup.html    # Setup screen
├── browser.json      # Auth credentials (not committed)
└── cookies.txt       # yt-dlp cookies (not committed)
```

---

## Troubleshooting

**ffmpeg not found?**
It auto-installs on first run. If that fails, run: `winget install ffmpeg`

**Authentication failed?**
- Make sure you're logged into music.youtube.com in your browser
- Close your browser and try again
- Try a different browser

**Rate limited by YouTube?**
Wait 30-60 minutes. Re-authenticate if needed.

**Songs not sorting by track number?**
Use the "Download All" button on an album page — this ensures correct track numbers are written to every file's metadata.

**Moving DECIBEL.exe?**
Move `browser.json` and `cookies.txt` with it, or re-authenticate.

---

## .gitignore

The following files should never be committed:

```
browser.json
cookies.txt
*.mp3
*.log
__pycache__/
*.pyc
venv/
build/
dist/
*.spec
.flask_secret_key
client_secret.json
oauth_token.json
debug_headers.log
```