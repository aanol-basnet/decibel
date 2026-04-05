# DECIBEL - Build Instructions

## Quick Start (Development)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the app (opens desktop window)
python launcher.py
```

---

## Build as Single .exe (Windows)

### Step 1: Install PyInstaller
```bash
pip install pyinstaller
```

### Step 2: Build the .exe
```bash
pyinstaller decibel.spec
```

### Step 3: Find your .exe
The built executable will be at:
```
dist/DECIBEL.exe
```

---

## Distribution

To share with friends:

1. Give them the `dist/DECIBEL.exe` file
2. On first run, they'll see the setup screen
3. They login to music.youtube.com in their browser
4. Select their browser and click "Authenticate"
5. Done! The app opens automatically

---

## How It Works

### First Launch:
1. User runs `DECIBEL.exe`
2. App checks if `browser.json` exists
3. If not, shows setup window
4. User selects browser and authenticates
5. App creates auth files and opens main UI

### Subsequent Launches:
1. User runs `DECIBEL.exe`
2. App verifies `browser.json` is valid
3. Opens main UI directly (no setup needed)

---

## File Structure After Build

```
decibel/
├── launcher.py          # Desktop app entry point
├── app.py               # Flask backend
├── static/
│   ├── index.html       # Main UI
│   └── setup.html       # Setup screen
├── requirements.txt     # Dependencies
├── setup_auth.py        # Standalone auth script (optional)
└── build.bat            # Build script (Windows)
```

After building:
```
decibel/
├── dist/
│   └── DECIBEL.exe      # Single file executable!
├── build/               # Build temp files (safe to delete)
└── ... (source files)
```

---

## Notes

- **WebView2**: Windows 10/11 already has it. Older Windows needs manual install.
- **Browser Support**: Chrome, Firefox, Edge, Brave, Opera
- **First Run**: User must be logged into music.youtube.com in their browser
- **Auth Files**: `browser.json` and `cookies.txt` created automatically in app folder
- **No Console**: The .exe runs as a pure desktop app (no command prompt)

---

## Troubleshooting

### "WebView2 not found"
User needs to install Microsoft Edge WebView2 Runtime:
https://developer.microsoft.com/en-us/microsoft-edge/webview2/

### "browser_cookie3 failed"
Make sure the user's browser is installed and they're logged into YouTube Music.

### "Port 5000 already in use"
Another app is using port 5000. Change it in `launcher.py`.
