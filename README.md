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
pip install yt-dlp flask flask-cors ytmusicapi mutagen requests --break-system-packages
```

**Windows** (run inside your virtual environment)
```bash
pip install yt-dlp flask flask-cors ytmusicapi mutagen requests
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

1. Open Chrome or Brave and go to [music.youtube.com](https://music.youtube.com) while logged in
2. Press `F12` to open DevTools → go to the **Network** tab
3. Press `F5` to reload, then filter by `browse`
4. Click any request, scroll to **Request Headers**, and copy all headers
5. In your terminal, run:
   ```bash
   ytmusicapi browser
   ```
6. Paste the headers and press Enter twice — this creates a `browser.json` file
7. Add `browser.json` to your `.gitignore` so it doesn't get pushed to GitHub:
   ```bash
   echo "browser.json" >> .gitignore
   ```

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