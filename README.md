# DECIBEL — Music Downloader

A clean, minimal music downloader with a browser-based UI. Search for artists, albums, and songs via YouTube Music, then download them as MP3s.

---

## Features

- Search artists, albums, and songs
- Browse artist pages and full album tracklists
- Download individual songs or entire albums
- Download queue — handles multiple downloads one at a time
- Saves as MP3 with metadata and album art embedded

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
git clone https://github.com/yourusername/decibel.git
cd decibel
```

---

### 3. Install Python dependencies

**Ubuntu / Debian / macOS**
```bash
pip install yt-dlp flask flask-cors ytmusicapi --break-system-packages
```

**Windows**
```bash
pip install yt-dlp flask flask-cors ytmusicapi
```

---

### 4. Fix PATH (Linux / macOS only)

If you get a `yt-dlp: command not found` error, run:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

---

## Running

```bash
python3 app.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

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

Files are saved to:

- **Linux / macOS**: `~/Music/Downloads`
- **Windows**: `C:\Users\<you>\Music\Downloads`

---

## Project Structure

```
decibel/
├── app.py           # Flask backend
└── static/
    └── index.html   # Frontend UI
```

---

## Troubleshooting

**Rate limited by YouTube?**
Wait 24 hours or use a cookies file. Export cookies from your browser using the "Get cookies.txt LOCALLY" extension, save as `cookies.txt` in the project folder, then add `--cookie-file cookies.txt` to the yt-dlp command in `app.py`.

**`yt-dlp` not found?**
Make sure `~/.local/bin` is on your PATH — see step 4 above.

**ffmpeg not found?**
Make sure ffmpeg is installed and on your PATH. Test with `ffmpeg -version`.