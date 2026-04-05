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

# Force pywebview to use QT backend (doesn't require pythonnet)
import os
os.environ["PYWEBVIEW_GUI"] = "qt"

import threading
import logging
import socket
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


def is_port_available(port, host='127.0.0.1'):
    """Check if a port is available for use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            return result != 0
    except Exception:
        return False


def find_available_port(start_port=5000, max_attempts=100):
    """Find an available port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        if is_port_available(port):
            return port
    return None


def check_webview2():
    """Check if WebView2 Runtime is installed."""
    try:
        import winreg
        # Check for WebView2 Runtime
        reg_paths = [
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ]
        for path in reg_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                    winreg.QueryValueEx(key, "pv")
                    return True
            except FileNotFoundError:
                continue
        
        # Also check if msedge.dll exists (WebView2 Runtime indicator)
        import pathlib
        edge_paths = [
            pathlib.Path(os.environ.get('ProgramFiles(x86)', '')) / 'Microsoft' / 'EdgeWebView' / 'Application',
            pathlib.Path(os.environ.get('ProgramFiles', '')) / 'Microsoft' / 'EdgeWebView' / 'Application',
        ]
        for edge_path in edge_paths:
            if edge_path.exists():
                return True
        
        return False
    except Exception:
        return False


def start_flask(port=5000):
    """Start Flask server in background thread."""
    # Bind to localhost only (not public network)
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False  # Disable reloader for desktop app
    )


def main():
    """Main entry point for DECIBEL desktop app."""
    logger.info("🎵 Starting DECIBEL Desktop...")

    # Check for WebView2 Runtime on Windows
    if sys.platform == 'win32' and not check_webview2():
        logger.warning("⚠️  WebView2 Runtime not detected")
        logger.info("💡 Download from: https://developer.microsoft.com/en-us/microsoft-edge/webview2/")
        logger.info("💡 The app will attempt to run, but may not work correctly")

    # Find an available port
    port = find_available_port(5000)
    if port is None:
        logger.error("❌ No available ports found. Cannot start Flask server.")
        logger.info("💡 Try closing other applications that may be using ports 5000-5100")
        sys.exit(1)

    if port != 5000:
        logger.info(f"⚠️  Port 5000 in use, using port {port} instead")

    # Start Flask in background thread
    flask_thread = threading.Thread(target=start_flask, args=(port,), daemon=True)
    flask_thread.start()

    # Wait for Flask to start
    import time
    time.sleep(1.5)

    logger.info(f"✅ Flask server running on http://127.0.0.1:{port}")
    logger.info("🖥️  Opening desktop window...")

    # Create native desktop window
    window = webview.create_window(
        "DECIBEL - Music Streaming",
        f"http://127.0.0.1:{port}",
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
