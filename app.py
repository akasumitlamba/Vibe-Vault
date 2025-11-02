import subprocess
import os
import json
import time
from flask import Flask, render_template, request, Response, send_from_directory

app = Flask(__name__)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(APP_ROOT, 'downloads')

if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)


def generate_sse_event(event_type, data):
    """Helper to format SSE data. Data should be a dictionary."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def stream_youtube_download(playlist_url, playlist_specific_download_path, sane_playlist_name):
    """Fetch playlist info, then download, yielding SSE events for UI updates."""

    yield generate_sse_event("status_message", {"message": "Fetching playlist details..."})
    playlist_items = []

    # === Stage 1: Fetch playlist info ===
    try:
        fetch_command = [
            "yt-dlp",
            "--flat-playlist",
            "--print-json",
            "--ignore-errors",
            "--socket-timeout", "30",
            "--no-warnings",
            playlist_url
        ]

        process_fetch = subprocess.Popen(
            fetch_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        for line in iter(process_fetch.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue

            try:
                video_info = json.loads(line)
                item = {
                    "id": video_info.get('id', f"item_{len(playlist_items)}"),
                    "title": video_info.get('title', 'Untitled Track'),
                    "playlist_index_original": video_info.get('playlist_index'),
                    "status": "queued"
                }
                playlist_items.append(item)
            except json.JSONDecodeError:
                continue

            yield generate_sse_event("status_message", {"message": f"Found {len(playlist_items)} items..."})

        process_fetch.wait()

        if not playlist_items:
            yield generate_sse_event("error_message", {"message": "No playlist items found or failed to parse."})
            yield generate_sse_event("stream_end", {"message": "Stream ended."})
            return

        MAX_SONGS = 40
        if len(playlist_items) > MAX_SONGS:
            yield generate_sse_event("error_message", {
                "message": f"Playlist has {len(playlist_items)} items. Max allowed is {MAX_SONGS}."
            })
            yield generate_sse_event("stream_end", {"message": "Stream ended due to playlist size limit."})
            return

        yield generate_sse_event("initial_song_list", {"songs": playlist_items})
        yield generate_sse_event("status_message", {"message": f"Found {len(playlist_items)} songs. Starting downloads..."})

    except Exception as e:
        yield generate_sse_event("error_message", {"message": f"Error fetching playlist: {str(e)}"})
        yield generate_sse_event("stream_end", {"message": "Stream ended."})
        return

    # === Stage 2: Download ===
    os.makedirs(playlist_specific_download_path, exist_ok=True)

    download_command = [
        "yt-dlp",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", os.path.join(playlist_specific_download_path, "%(playlist_index)s - %(title)s.%(ext)s"),
        "--yes-playlist", "--ignore-errors",
        "--socket-timeout", "30", "--retries", "3", "--no-warnings",
        "--progress", "--newline",
        playlist_url
    ]

    process_download = subprocess.Popen(
        download_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True
    )

    songs_done = 0
    total = len(playlist_items)
    download_start = time.time()
    last_update = time.time()

    for line in iter(process_download.stdout.readline, ""):
        line = line.strip()
        if not line:
            continue

        # Timeout safeguard
        if time.time() - download_start > 3600:
            yield generate_sse_event("error_message", {"message": "Download timed out after 1 hour."})
            process_download.terminate()
            break

        # Update activity timestamp
        last_update = time.time()

        # Display raw yt-dlp progress
        if "[download]" in line and "%" in line:
            yield generate_sse_event("status_message", {"message": line})

        # Detect when a file is finished
        if "[ExtractAudio]" in line and ".mp3" in line:
            songs_done += 1
            percent = round((songs_done / total) * 100)
            yield generate_sse_event("overall_progress", {
                "downloaded_count": songs_done,
                "total_count": total,
                "percentage": percent
            })
            yield generate_sse_event("status_message", {"message": f"Downloaded {songs_done}/{total}"})

        # Handle error
        if "ERROR:" in line.upper():
            yield generate_sse_event("error_message", {"message": line})

    process_download.wait()

    yield generate_sse_event("status_message", {"message": "Download process completed."})
    yield generate_sse_event("stream_end", {"message": "All done!"})


@app.after_request
def add_no_buffer_headers(response):
    """Disable buffering to ensure SSE messages stream immediately."""
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route('/')
def index_page():
    return render_template('index.html')


@app.route('/stream-download')
def stream_download_route():
    playlist_url = request.args.get('playlist_url')
    playlist_name = request.args.get('playlist_name', 'playlist')

    if not playlist_url:
        def error_stream():
            yield generate_sse_event("error_message", {"message": "Playlist URL is required."})
            yield generate_sse_event("stream_end", {"message": "Stream ended due to missing URL."})
        return Response(error_stream(), mimetype='text/event-stream')

    sane_playlist_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in playlist_name).rstrip().replace(' ', '_')
    if not sane_playlist_name:
        sane_playlist_name = "downloaded_playlist"

    playlist_specific_download_path = os.path.join(DOWNLOADS_DIR, sane_playlist_name)

    return Response(
        stream_youtube_download(playlist_url, playlist_specific_download_path, sane_playlist_name),
        mimetype='text/event-stream'
    )


@app.route('/downloads/<playlist_folder>/<filename>')
def downloaded_file(playlist_folder, filename):
    file_path = os.path.join(DOWNLOADS_DIR, playlist_folder)
    return send_from_directory(directory=file_path, path=filename, as_attachment=True)


if __name__ == '__main__':
    if not os.path.exists(DOWNLOADS_DIR):
        os.makedirs(DOWNLOADS_DIR)
    # Disable debug reloader for clean SSE streaming
    app.run(debug=False, threaded=True, port=5000)
