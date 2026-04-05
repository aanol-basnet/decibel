# Authentication Redirect Fix

## Problem

After successful authentication on the setup screen, the page **refreshed** instead of redirecting to the main app.

## Root Cause

The `is_auth_valid()` function uses a **5-minute TTL cache** (`AUTH_CACHE_TTL = 300 seconds`) to avoid repeated YTMusic API calls.

**The flow was:**
1. User authenticates on setup screen
2. `browser.json` file gets created
3. Frontend redirects to `/` after 2 seconds
4. `index()` route calls `is_auth_valid()`
5. **Problem**: `is_auth_valid()` returns cached `False` value (from before authentication)
6. User sees setup page again (refreshed)

## Solution

Clear the auth cache immediately after successful authentication so the next `is_auth_valid()` call performs a fresh check.

### Changes Made

**File**: `app.py` (lines 334-353)

**Before:**
```python
@app.route("/setup/authenticate", methods=["POST"])
def setup_authenticate():
    data = request.get_json()
    browser = data.get("browser", "").lower()
    
    if not browser:
        return jsonify({"success": False, "error": "Please select a browser"}), 400
    
    result = extract_browser_cookies(browser)
    
    if result["success"]:
        initialize_ytmusic_with_token()
    
    return jsonify(result)
```

**After:**
```python
@app.route("/setup/authenticate", methods=["POST"])
def setup_authenticate():
    data = request.get_json()
    browser = data.get("browser", "").lower()
    
    if not browser:
        return jsonify({"success": False, "error": "Please select a browser"}), 400
    
    result = extract_browser_cookies(browser)
    
    if result["success"]:
        initialize_ytmusic_with_token()
        
        # Clear auth cache so the new authentication is immediately recognized
        clear_auth_cache()
        logger.info("✅ Auth cache cleared after successful authentication")
    
    return jsonify(result)
```

## How It Works Now

**Correct Flow:**
1. User authenticates on setup screen
2. `browser.json` file gets created
3. `initialize_ytmusic_with_token()` reinitializes YTMusic
4. **NEW**: `clear_auth_cache()` resets the cache
5. Frontend redirects to `/` after 2 seconds
6. `index()` calls `is_auth_valid()`
7. `is_auth_valid()` performs fresh check (no cache)
8. Finds `browser.json`, validates it, returns `True` ✅
9. User sees main app ✅

## Cache Details

**Auth Cache Configuration:**
```python
_auth_cache = {"valid": None, "timestamp": 0}
AUTH_CACHE_TTL = 300  # 5 minutes
```

**Cache Behavior:**
- **Before fix**: Cache persisted for 5 minutes even after new authentication
- **After fix**: Cache cleared immediately after successful authentication
- **Benefit**: No delay in recognizing new auth status

## Testing

### Test Steps:
1. Launch DECIBEL
2. If not authenticated, setup screen appears
3. Select browser and click "Authenticate"
4. Wait for "Authentication setup complete" message
5. After 2 seconds, should redirect to main app ✅

### Expected Logs:
```
✅ Browser authentication verified successfully
✅ Auth cache cleared after successful authentication
✅ YTMusic initialized with browser.json
```

### Success Indicators:
- ✅ No page refresh after authentication
- ✅ Smooth redirect to main app
- ✅ Main app loads with authenticated content
- ✅ No 5-minute delay

## Related Functions

### `is_auth_valid()` (lines 1857-1883)
- Checks if `browser.json` exists
- Validates by calling YTMusic API
- Caches result for 5 minutes
- **Now works correctly** after auth cache is cleared

### `clear_auth_cache()` (lines 1885-1889)
```python
def clear_auth_cache():
    """Clear authentication cache (e.g., after logout)."""
    global _auth_cache
    _auth_cache["valid"] = None
    _auth_cache["timestamp"] = 0
```
- Resets cache to initial state
- Forces next `is_auth_valid()` call to perform fresh check

### `initialize_ytmusic_with_token()` (lines 404-436)
- Reinitializes global `ytmusic` instance
- Tries OAuth token first
- Falls back to `browser.json`
- Falls back to unauthenticated mode

## Other Cache Clearing Points

Auth cache is also cleared in:
- **Logout** (line 585): When user logs out, cache cleared
- **Clear auth cache endpoint** (line 1885): Manual cache clearing

## Build Status

✅ Build successful  
✅ Application launches correctly  
✅ Auth cache fix implemented  
✅ No breaking changes

---

**Key Fix**: Added `clear_auth_cache()` call after successful authentication to ensure immediate redirect to main app.
