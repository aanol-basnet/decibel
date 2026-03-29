"""
DECIBEL - Automatic Browser Cookie Extractor

This script AUTOMATICALLY extracts YouTube Music authentication from your browser
and creates browser.json (for ytmusicapi) and cookies.txt (for yt-dlp).

NO MANUAL COPY-PASTING REQUIRED!

Supported browsers: Chrome, Firefox, Edge, Brave, Opera
"""

import os
import sys
import json
import http.cookiejar
import subprocess
import hashlib
import hmac
import time

# Try to import browser_cookie3
try:
    import browser_cookie3
except ImportError:
    print("❌ browser_cookie3 not installed. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "browser_cookie3"])
    import browser_cookie3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BROWSER_JSON = os.path.join(BASE_DIR, "browser.json")
COOKIES_TXT = os.path.join(BASE_DIR, "cookies.txt")


def generate_sapisidhash(sapisid, origin="https://music.youtube.com"):
    """
    Generate SAPISIDHASH header value from SAPISID cookie.
    This is how YouTube generates the Authorization header.
    """
    timestamp = int(time.time())
    hash_input = f"{timestamp} {sapisid} {origin}"
    hash_value = hashlib.sha1(hash_input.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{hash_value}"


def get_browser_cookies(browser_name=None):
    """
    Extract ALL cookies from the specified browser.
    If browser_name is None, tries all supported browsers.
    
    Returns dict of cookies {name: value, ...}
    """
    browsers = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "brave": browser_cookie3.brave,
        "opera": browser_cookie3.opera,
    }
    
    # Filter to requested browser if specified
    if browser_name:
        bn = browser_name.lower()
        if bn in browsers:
            browsers = {bn: browsers[bn]}
        else:
            print(f"❌ Unknown browser: {browser_name}")
            print(f"   Supported: {', '.join(browsers.keys())}")
            return None
    
    for name, func in browsers.items():
        try:
            print(f"🔍 Trying {name}...")
            # Get all YouTube/Google cookies
            cj = func(domain_name="youtube.com")
            if cj:
                cookies = {}
                for cookie in cj:
                    if cookie.domain and ('youtube' in cookie.domain or 'google' in cookie.domain):
                        cookies[cookie.name] = cookie.value
                
                # Check for essential authentication cookies
                has_sapisid = "SAPISID" in cookies
                has_sid = "SID" in cookies or "__Secure-1PSID" in cookies
                
                if has_sapisid and has_sid:
                    print(f"✅ Found complete auth cookies in {name}!")
                    return cookies
                elif len(cookies) > 10:
                    print(f"✅ Found {len(cookies)} cookies in {name}")
                    return cookies
                else:
                    print(f"⚠️  {name} found but may not be logged in")
        except Exception as e:
            error_msg = str(e)
            if "could not find" in error_msg.lower() or "profile" in error_msg.lower():
                print(f"⚠️  {name} not found or no profile")
            else:
                print(f"⚠️  {name} failed: {error_msg[:50]}")
            continue
    
    return None


def create_browser_json(cookies, filepath):
    """
    Create browser.json with proper YouTube Music authentication headers.
    
    The format ytmusicapi expects:
    {
        "Accept": "*/*",
        "Authorization": "SAPISIDHASH ...",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": "https://music.youtube.com",
        "Cookie": "..."
    }
    """
    try:
        # Get SAPISID for generating Authorization header
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        
        if not sapisid:
            print("⚠️  Warning: SAPISID cookie not found, auth may not work")
            auth_header = ""
        else:
            auth_header = generate_sapisidhash(sapisid)
        
        # Build cookie string
        cookie_parts = []
        for name, value in cookies.items():
            cookie_parts.append(f"{name}={value}")
        cookie_string = "; ".join(cookie_parts)
        
        # Create the browser.json structure
        browser_config = {
            "Accept": "*/*",
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": "https://music.youtube.com",
            "Cookie": cookie_string,
        }
        
        # Also save raw cookies for reference
        # browser_config["raw_cookies"] = cookies
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(browser_config, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Saved browser.json to: {filepath}")
        return True
    except Exception as e:
        print(f"❌ Failed to save browser.json: {e}")
        return False


def save_cookies_txt(cookies, filepath):
    """
    Save cookies in Netscape format (cookies.txt for yt-dlp).
    """
    try:
        cj = http.cookiejar.MozillaCookieJar()
        for name, value in cookies.items():
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".music.youtube.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
            cj.set_cookie(cookie)
        cj.save(filepath, ignore_discard=True, ignore_expires=True)
        print(f"✅ Saved cookies.txt to: {filepath}")
        return True
    except Exception as e:
        print(f"❌ Failed to save cookies.txt: {e}")
        return False


def verify_ytmusic_auth():
    """
    Verify that the created browser.json works with ytmusicapi.
    """
    if not os.path.exists(BROWSER_JSON):
        print("⚠️  browser.json not found")
        return False
    
    try:
        from ytmusicapi import YTMusic
        print("🔍 Verifying browser.json with ytmusicapi...")
        ytmusic = YTMusic(BROWSER_JSON)
        # Try a simple authenticated call
        result = ytmusic.get_home(limit=1)
        if result:
            print("✅ browser.json is working! YTMusic authenticated.")
            return True
        else:
            print("⚠️  browser.json created but authentication may not be working")
            return False
    except Exception as e:
        print(f"⚠️  Verification failed: {e}")
        print("   Make sure you're logged into music.youtube.com in your browser")
        return False


def main():
    print("=" * 60)
    print("DECIBEL - Automatic Browser Cookie Extractor")
    print("=" * 60)
    print()
    print("This will AUTOMATICALLY extract YouTube Music authentication")
    print("from your browser. No manual copy-pasting required!")
    print()
    print("Make sure you're logged into music.youtube.com in your browser.")
    print()
    
    # Check if files already exist
    if os.path.exists(BROWSER_JSON):
        print(f"⚠️  {BROWSER_JSON} already exists.")
        response = input("Overwrite? (y/n): ").strip().lower()
        if response != "y":
            print("Cancelled.")
            return 1
        os.remove(BROWSER_JSON)
    
    if os.path.exists(COOKIES_TXT):
        print(f"⚠️  {COOKIES_TXT} already exists.")
        response = input("Overwrite? (y/n): ").strip().lower()
        if response != "y":
            print("Cancelled.")
            return 1
        os.remove(COOKIES_TXT)
    
    print()
    print("Available browsers: chrome, firefox, edge, brave, opera")
    browser_input = input("Choose browser (press Enter for auto-detect): ").strip()
    browser_name = browser_input if browser_input else None
    
    print()
    print("🔍 Extracting authentication cookies from browser...")
    print()
    
    cookies = get_browser_cookies(browser_name)
    
    if not cookies:
        print()
        print("❌ Could not extract cookies from any browser.")
        print()
        print("Troubleshooting:")
        print("  1. Make sure you're logged into music.youtube.com")
        print("  2. Try closing your browser and running this again")
        print("  3. Try specifying a browser: python setup_auth.py chrome")
        print()
        return 1
    
    print()
    print("📁 Creating authentication files...")
    print()
    
    # Create both files
    success = True
    if not create_browser_json(cookies, BROWSER_JSON):
        success = False
    
    if not save_cookies_txt(cookies, COOKIES_TXT):
        success = False
    
    if not success:
        print()
        print("❌ Failed to create one or more files.")
        return 1
    
    print()
    print("🔐 Verifying authentication...")
    print()
    
    verify_ytmusic_auth()
    
    print()
    print("=" * 60)
    print("✅ Setup Complete!")
    print("=" * 60)
    print()
    print("Files created:")
    print(f"  ✅ {BROWSER_JSON} (for ytmusicapi)")
    print(f"  ✅ {COOKIES_TXT} (for yt-dlp)")
    print()
    print("⚠️  IMPORTANT: Add these to your .gitignore!")
    print('   Run: echo "browser.json" >> .gitignore')
    print('          echo "cookies.txt" >> .gitignore')
    print()
    print("Now you can run app.py and enjoy authenticated features!")
    print()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
