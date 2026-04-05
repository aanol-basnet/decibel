"""
Runtime hook for DECIBEL - Force import yt_dlp before app starts
"""
import sys
try:
    import yt_dlp
except ImportError as e:
    print(f"CRITICAL: yt_dlp import failed: {e}", file=sys.stderr)
    raise
