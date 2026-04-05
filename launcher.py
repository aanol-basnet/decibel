"""
DECIBEL - Desktop Launcher
Launches the Flask app in a native desktop window using pywebview.
"""

import os
import sys

# Fix paths for PyInstaller frozen environment
if getattr(sys, 'frozen', False):
    # PyInstaller extracts to _MEIPASS, add it to path
    if hasattr(sys, '_MEIPASS'):
        if sys._MEIPASS not in sys.path:
            sys.path.insert(0, sys._MEIPASS)
        # Also set working directory to MEIPASS so relative paths work
        os.chdir(sys._MEIPASS)

# Disable gettext locale lookups BEFORE any other imports
os.environ["LANG"] = "C"
os.environ["LANGUAGE"] = ""
os.environ["LC_ALL"] = "C"

# Monkey-patch gettext to gracefully handle missing translation files
import gettext
_original_translation = gettext.translation
def _safe_translation(domain, localedir=None, languages=None, *args, **kwargs):
    try:
        return _original_translation(domain, localedir, languages, *args, **kwargs)
    except FileNotFoundError:
        return gettext.NullTranslations()
gettext.translation = _safe_translation

import threading
import logging
import webview

# Import app module
try:
    from app import app
except ImportError as e:
    # In frozen environment, try loading app.py from _MEIPASS
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        import importlib.util
        app_path = os.path.join(sys._MEIPASS, 'app.py')
        if os.path.exists(app_path):
            spec = importlib.util.spec_from_file_location('app', app_path)
            app_module = importlib.util.module_from_spec(spec)
            sys.modules['app'] = app_module
            spec.loader.exec_module(app_module)
            app = app_module.app
        else:
            print(f"ERROR: app.py not found at {app_path}")
            print(f"Contents of _MEIPASS ({sys._MEIPASS}):")
            for f in os.listdir(sys._MEIPASS):
                print(f"  {f}")
            sys.exit(1)
    else:
        print(f"ERROR: Failed to import app module: {e}")
        print(f"frozen={getattr(sys, 'frozen', False)}, _MEIPASS={getattr(sys, '_MEIPASS', 'N/A')}")
        print(f"sys.path={sys.path[:5]}...")
        sys.exit(1)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def start_flask():
    """Start Flask server in background thread."""
    # Bind to localhost only (not public network)
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False  # Disable reloader for desktop app
    )


def main():
    """Main entry point for DECIBEL desktop app."""
    logger.info("🎵 Starting DECIBEL Desktop...")

    # Start Flask in background thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Wait for Flask to start
    import time
    time.sleep(1.5)

    logger.info("✅ Flask server running on http://127.0.0.1:5000")
    logger.info("🖥️  Opening desktop window...")

    # Create native desktop window
    window = webview.create_window(
        "DECIBEL - Music Streaming",
        "http://127.0.0.1:5000",
        width=1200,
        height=800,
        resizable=True,
        min_size=(900, 600),
    )

    # Start pywebview main loop (this blocks until window closes)
    try:
        webview.start(debug=False)
    except Exception as e:
        logger.error(f"Webview error: {e}")
    finally:
        logger.info("DECIBEL closed")


if __name__ == "__main__":
    main()
