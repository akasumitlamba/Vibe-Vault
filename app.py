"""
Improved Flask app for streaming YouTube playlist downloads via yt-dlp using Server-Sent Events (SSE).
Features improved error handling, logging, filename sanitization, process management, heartbeat pings,
size and time limits, and clearer SSE events for the frontend.

Notes:
 - Requires `yt-dlp` and `ffmpeg` on PATH for audio extraction.
 - Designed to be a single-file app you can run with: `python flask_yt_dlp_sse_improved.py`

"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, List, Optional

from flask import (
    Flask,
    Response,
    abort,
    render_template,
    request,
    safe_join,
    send_from_directory,
    stream_with_context,
)

# -------------------------
# Configuration
# -------------------------
APP_ROOT = Path(__file__).parent.resolve()
DOWNLOADS_DIR = APP_ROOT / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

MAX_SONGS = 40
DOWNLOAD_TIMEOUT_SECONDS = 60 * 60  # 1 hour per overall download operation
FETCH_TIMEOUT_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 15
YTDLP_BIN = shutil.which("yt-dlp") or shutil.which("yt_dlp")
FFMPEG_BIN = shutil.which("ffmpeg")

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("yt_streamer")

# -------------------------
# Utilities
# -------------------------

SSE_EVENT_TEMPLATE = "event: {event}\n{data}\n\n"


def sse_event(event: str, data: Dict) -> str:
    """Return a properly-formatted SSE event string. JSON-encodes `data`."""
    # Use `data:` lines: break JSON into multiple lines if necessary (SSE spec)
    json_text = json.dumps(data, ensure_ascii=False)
    # prefix every line of the payload with `data: `
    data_lines = "\n".join(f"data: {line}" for line in json_text.splitlines())
    return f"event: {event}\n{data_lines}\n\n"


def sanitize_name(name: str) -> str:
    """Create a filesystem-safe short name (underscores, dashes allowed)."""
    name = name.strip()
    # Replace disallowed characters with underscore
    safe = re.sub(r"[^A-Za-z0-9 _\-\.]+", "_", name)
    # Collapse whitespace to underscore
    safe = re.sub(r"\s+", "_", safe)
    # Prevent empty names
    return safe or "downloaded_playlist"


def check_requirements() -> Optional[str]:
    """Return a human-friendly message if required executables are missing, else None."""
    missing = []
    if not YTDLP_BIN:
        missing.append("yt-dlp")
    if not FFMPEG_BIN:
        # ffmpeg is required only when extracting audio; warn but not fatal here
        logger.warning("ffmpeg not found on PATH — audio extraction may fail")
    if missing:
        return ", ".join(missing)
    return None


@contextmanager
def _popen_guard(proc: subprocess.Popen):
    """Context manager that ensures the subprocess is terminated on exit (or on SIGTERM).

    Yields the process object.
    """
    def _terminate_process(_signum=None, _frame=None):
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass

    old_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _terminate_process)
    try:
        yield proc
n    finally:
        signal.signal(signal.SIGTERM, old_handler)
        try:
            if proc.poll() is None:
                proc.terminate()
                # give it a moment then kill
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass


# -------------------------
# Core streaming logic
# -------------------------


def fetch_playlist_items(playlist_url: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> List[Dict]:
    """Fetch playlist metadata using yt-dlp in flat-playlist/print-json mode.

    Returns a list of items with keys: id, title, playlist_index (optional).
    Raises RuntimeError on failure.
    """
    if not YTDLP_BIN:
        raise RuntimeError("yt-dlp not found on PATH")

    cmd = [
        YTDLP_BIN,
        "--flat-playlist",
        "--print-json",
        "--no-warnings",
        "--socket-timeout",
        str(timeout),
        playlist_url,
    ]
    logger.info("Fetching playlist with command: %s", cmd)

    items: List[Dict] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    with _popen_guard(proc):
        # Iterate lines as they are produced
        for raw in iter(proc.stdout.readline, ""):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                items.append(
                    {
                        "id": obj.get("id") or f"item_{len(items)}",
                        "title": obj.get("title", "Untitled Track"),
                        "playlist_index": obj.get("playlist_index"),
                    }
                )
            except json.JSONDecodeError:
                # ignore non-json lines from yt-dlp
                logger.debug("Skipping non-json line from yt-dlp: %s", line)
                continue

    if proc.returncode and not items:
        raise RuntimeError("yt-dlp exited with non-zero status and returned no items")

    return items


def parse_download_progress_line(line: str) -> Optional[Dict]:
    """Attempt to parse useful progress info from a yt-dlp stdout line.

    Returns a dict like {"type": "download_progress", ...} or None.
    """
    # Common patterns: [download]  12.3% of 3.75MiB at 123.45KiB/s ETA 00:01
    if "[download]" in line and "%" in line:
        m = re.search(r"(\d{1,3}\.\d|\d{1,3})%", line)
        percent = None
        if m:
            try:
                percent = float(m.group(1))
            except Exception:
                percent = None
        return {"type": "download_progress", "raw": line, "percent": percent}

    # ExtractAudio finished message example: [ExtractAudio] Destination: 01 - Song Title.mp3
    if "[ExtractAudio]" in line and ".mp3" in line:
        m = re.search(r"Destination:\s*(.+\.mp3)", line)
        return {"type": "extract_done", "filename": m.group(1) if m else None}

    # Generic ERROR detection
    if "ERROR:" in line.upper():
        return {"type": "error", "raw": line}

    return None


def stream_youtube_download(playlist_url: str, playlist_folder: Path, frontend_name: str) -> Generator[str, None, None]:
    """Generator that yields SSE events while fetching metadata and running yt-dlp to download audio.

    Yields SSE-compatible strings.
    """
    start_ts = time.time()

    # quick requirement check
    missing = check_requirements()
    if missing:
        yield sse_event("error", {"message": f"Missing required binaries: {missing}"})
        yield sse_event("stream_end", {"message": "aborted"})
        return

    yield sse_event("status", {"message": "Fetching playlist details..."})

    try:
        items = fetch_playlist_items(playlist_url)
    except Exception as exc:
        logger.exception("Failed to fetch playlist")
        yield sse_event("error", {"message": f"Failed to fetch playlist: {str(exc)}"})
        yield sse_event("stream_end", {"message": "aborted"})
        return

    if not items:
        yield sse_event("error", {"message": "No playlist items found."})
        yield sse_event("stream_end", {"message": "aborted"})
        return

    if len(items) > MAX_SONGS:
        yield sse_event("error", {"message": f"Playlist has {len(items)} items; limit is {MAX_SONGS}."})
        yield sse_event("stream_end", {"message": "aborted"})
        return

    # Normalize and send initial list
    for idx, it in enumerate(items, start=1):
        it.setdefault("playlist_index", idx)
        it.setdefault("status", "queued")

    yield sse_event("initial_song_list", {"songs": items})
    yield sse_event("status", {"message": f"Starting download of {len(items)} items..."})

    # Ensure playlist folder exists
    playlist_folder.mkdir(parents=True, exist_ok=True)

    # Build download command
    out_template = str(playlist_folder / "%(playlist_index)03d - %(title)s.%(ext)s")
    cmd = [
        YTDLP_BIN,
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "-o",
        out_template,
        "--yes-playlist",
        "--ignore-errors",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
        "--no-warnings",
        "--newline",
        playlist_url,
    ]

    logger.info("Starting download: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    songs_done = 0
    total = len(items)
    last_heartbeat = time.time()
    download_start = time.time()

    with _popen_guard(proc):
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\n")
            if not line:
                continue

            # Heartbeat
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS:
                yield sse_event("heartbeat", {"ts": int(time.time())})
                last_heartbeat = time.time()

            # Timeout overall
            if time.time() - download_start > DOWNLOAD_TIMEOUT_SECONDS:
                yield sse_event("error", {"message": "Download timed out"})
                proc.terminate()
                break

            parsed = parse_download_progress_line(line)
            if parsed is None:
                # Send some raw progress lines occasionally for frontend debugging
                if "[ffmpeg]" in line or "Destination:" in line or "Deleting original file" in line:
                    yield sse_event("status", {"message": line})
                continue

            if parsed["type"] == "download_progress":
                yield sse_event("download_progress", {"raw": parsed["raw"], "percent": parsed.get("percent")})

            elif parsed["type"] == "extract_done":
                songs_done += 1
                percent = round((songs_done / total) * 100)
                yield sse_event(
                    "song_complete",
                    {
                        "downloaded_count": songs_done,
                        "total_count": total,
                        "percentage": percent,
                        "filename": parsed.get("filename"),
                    },
                )

            elif parsed["type"] == "error":
                yield sse_event("error", {"message": parsed.get("raw")})

    # wait for exit and read remaining output
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass

    yield sse_event("status", {"message": "Download finished."})
    yield sse_event("stream_end", {"message": "done"})


# -------------------------
# Flask app
# -------------------------

app = Flask(__name__)


@app.after_request
def add_no_buffer_headers(response):
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/")
def index_page():
    # If you have an HTML frontend put it in templates/index.html; otherwise return a brief help string.
    if (APP_ROOT / "templates" / "index.html").exists():
        return render_template("index.html")
    return (
        "<h3>yt-dlp SSE download server</h3>"
        "<p>Use /stream-download?playlist_url=...&playlist_name=... to start a streamed download.</p>"
    )


@app.route("/stream-download")
def stream_download_route():
    playlist_url = request.args.get("playlist_url")
    playlist_name = request.args.get("playlist_name", "playlist")

    if not playlist_url:
        def _error():
            yield sse_event("error", {"message": "playlist_url parameter required"})
            yield sse_event("stream_end", {"message": "aborted"})

        return Response(_error(), mimetype="text/event-stream")

    safe_name = sanitize_name(playlist_name)
    playlist_folder = DOWNLOADS_DIR / safe_name

    # Wrap generator with stream_with_context so request context is available during streaming
    gen = stream_youtube_download(playlist_url, playlist_folder, safe_name)

    return Response(stream_with_context(gen), mimetype="text/event-stream")


@app.route("/downloads/<playlist_folder>/<path:filename>")
def downloaded_file(playlist_folder: str, filename: str):
    # safe join to avoid directory traversal
    try:
        target_dir = safe_join(str(DOWNLOADS_DIR), playlist_folder)
    except Exception:
        abort(404)

    full_path = Path(target_dir) / filename
    if not full_path.exists():
        abort(404)

    # send as attachment
    return send_from_directory(directory=target_dir, path=filename, as_attachment=True)


if __name__ == "__main__":
    missing = check_requirements()
    if missing:
        logger.error("Missing required binaries: %s", missing)
        print("ERROR: Missing required binaries: ", missing)
        # still allow app to start so user can read message in browser

    # Disable the debug reloader (it spawns extra processes that interfere with streaming)
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
