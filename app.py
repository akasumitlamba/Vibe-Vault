import subprocess
import os
import json
import time
# import re # No longer needed
from flask import Flask, render_template, request, Response, url_for, send_from_directory


app = Flask(__name__)
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(APP_ROOT, 'downloads')

if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

def generate_sse_event(event_type, data):
    """Helper to format SSE data. Data should be a dictionary."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def stream_youtube_download(playlist_url, playlist_specific_download_path, sane_playlist_name):
    """ Fetches playlist info, then downloads, yielding SSE events for detailed UI. """
    
    # === Stage 1: Fetch Playlist Information ===
    yield generate_sse_event("status_message", {"message": "Fetching playlist details..."})
    playlist_items = []
    try:
        # Use yt-dlp to get playlist info as JSON objects, one per line
        fetch_command = [
            'yt-dlp',
            '--flat-playlist', # Don't extract info from individual videos yet
            '--print-json',
            '--ignore-errors',
            '--socket-timeout', '30',  # Add timeout for network operations
            '--no-warnings',  # Reduce noise in output
            playlist_url
        ]
        process_fetch = subprocess.Popen(fetch_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout_fetch, stderr_fetch = process_fetch.communicate(timeout=60)  # Add timeout for the entire fetch process

        if process_fetch.returncode != 0:
            err_msg = f"Failed to fetch playlist details. yt-dlp stderr: {stderr_fetch.strip()}"
            yield generate_sse_event("error_message", {"message": err_msg})
            yield generate_sse_event("stream_end", {"message": "Stream ended due to fetch error."})
            return

        for line in stdout_fetch.strip().split('\n'):
            if line:
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
                    continue  # Skip malformed lines silently
        
        if not playlist_items:
            yield generate_sse_event("error_message", {"message": "No items found in the playlist or failed to parse details."})
            yield generate_sse_event("stream_end", {"message": "Stream ended, no items."})
            return

        # Check for playlist size limit
        MAX_SONGS = 40
        if len(playlist_items) > MAX_SONGS:
            yield generate_sse_event("error_message", {
                "message": f"Playlist has {len(playlist_items)} songs. Maximum allowed is {MAX_SONGS} songs. Please select a smaller playlist."
            })
            yield generate_sse_event("stream_end", {"message": "Stream ended due to playlist size limit."})
            return

        yield generate_sse_event("initial_song_list", {"songs": playlist_items})
        yield generate_sse_event("overall_progress", {"downloaded_count": 0, "total_count": len(playlist_items), "percentage": 0})
        yield generate_sse_event("status_message", {"message": f"Found {len(playlist_items)} songs. Starting downloads..."})

    except subprocess.TimeoutExpired:
        yield generate_sse_event("error_message", {"message": "Timeout while fetching playlist details. Please try again."})
        yield generate_sse_event("stream_end", {"message": "Stream ended due to timeout."})
        return
    except Exception as e:
        yield generate_sse_event("error_message", {"message": f"Error fetching playlist details: {str(e)}"})
        yield generate_sse_event("stream_end", {"message": "Stream ended due to fetch error."})
        return

    # === Stage 2: Download and Process ===
    if not os.path.exists(playlist_specific_download_path):
        try:
            os.makedirs(playlist_specific_download_path)
        except OSError as e:
            yield generate_sse_event("error_message", {"message": f"Failed to create directory {playlist_specific_download_path}: {e}"})
            yield generate_sse_event("stream_end", {"message": "Stream ended due to directory error."})
            return

    download_command = [
        'yt-dlp',
        '-x', # Extract audio
        '--audio-format', 'mp3',
        '--audio-quality', '0',
        '-o', os.path.join(playlist_specific_download_path, '%(playlist_index)s - %(title)s.%(ext)s'),
        '--yes-playlist',
        '--ignore-errors',
        '--socket-timeout', '30',  # Add timeout for network operations
        '--retries', '3',  # Add retry attempts
        '--fragment-retries', '3',  # Add retry attempts for fragments
        '--file-access-retries', '3',  # Add retry attempts for file access
        '--no-warnings',  # Reduce noise in output
        '--progress',  # Show progress
        '--newline',
        playlist_url
    ]

    process_download = None
    songs_processed_count = 0
    last_progress_update = time.time()
    progress_update_interval = 2  # Update progress every 2 seconds
    last_activity_time = time.time()
    activity_timeout = 300  # 5 minutes without activity before considering it stuck

    try:
        process_download = subprocess.Popen(download_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
        
        expected_yt_dlp_playlist_index = 1
        download_start_time = time.time()
        max_download_time = 3600  # 1 hour maximum download time

        if process_download.stdout:
            for line in iter(process_download.stdout.readline, ''):
                current_time = time.time()
                
                # Check for overall timeout
                if current_time - download_start_time > max_download_time:
                    yield generate_sse_event("error_message", {"message": "Download process timed out after 1 hour. Please try again with a smaller playlist or better connection."})
                    process_download.terminate()
                    break

                # Check for activity timeout
                if current_time - last_activity_time > activity_timeout:
                    yield generate_sse_event("error_message", {"message": "Download appears to be stuck. Please try again."})
                    process_download.terminate()
                    break

                line = line.strip()
                if not line:
                    continue

                # Update last activity time if we see any output
                last_activity_time = current_time

                # Update progress periodically
                if current_time - last_progress_update >= progress_update_interval:
                    yield generate_sse_event("overall_progress", {
                        "downloaded_count": songs_processed_count,
                        "total_count": len(playlist_items),
                        "percentage": round((songs_processed_count / len(playlist_items)) * 100)
                    })
                    last_progress_update = current_time

                current_song_to_update = None
                song_index_in_our_list = expected_yt_dlp_playlist_index - 1

                if 0 <= song_index_in_our_list < len(playlist_items):
                    current_song_to_update = playlist_items[song_index_in_our_list]
                
                if current_song_to_update and current_song_to_update["status"] == "queued":
                    yield generate_sse_event("song_status_update", {
                        "id": current_song_to_update["id"],
                        "status": "downloading",
                        "title": current_song_to_update["title"]
                    })
                    current_song_to_update["status"] = "downloading"
                    yield generate_sse_event("status_message", {"message": f"Downloading: {current_song_to_update['title']}"})

                if "[ExtractAudio] Destination:" in line and line.endswith(".mp3"):
                    try:
                        filename_part = line.split("Destination:",1)[-1].strip()
                        base_filename = os.path.basename(filename_part)
                        
                        parts = base_filename.split(' - ', 1)
                        dl_playlist_idx_str = parts[0]
                        dl_playlist_idx = int(dl_playlist_idx_str)
                        target_song_index = dl_playlist_idx - 1

                        if 0 <= target_song_index < len(playlist_items):
                            song_obj = playlist_items[target_song_index]
                            if song_obj["status"] != "completed":
                                song_obj["status"] = "completed"
                                songs_processed_count += 1
                                
                                yield generate_sse_event("song_status_update", {
                                    "id": song_obj["id"],
                                    "status": "completed",
                                    "title": song_obj["title"]
                                })
                                
                                yield generate_sse_event("overall_progress", {
                                    "downloaded_count": songs_processed_count,
                                    "total_count": len(playlist_items),
                                    "percentage": round((songs_processed_count / len(playlist_items)) * 100)
                                })
                                
                                expected_yt_dlp_playlist_index = dl_playlist_idx + 1
                    except Exception as parse_ex:
                        yield generate_sse_event("error_message", {"message": f"Error processing download output: {str(parse_ex)}"})
                
                elif "ERROR:" in line.upper():
                    if current_song_to_update and current_song_to_update["status"] == "downloading":
                        current_song_to_update["status"] = "failed"
                        songs_processed_count += 1
                        yield generate_sse_event("song_status_update", {
                            "id": current_song_to_update["id"],
                            "status": "failed",
                            "title": current_song_to_update["title"]
                        })
                        yield generate_sse_event("error_message", {"message": f"Failed: {current_song_to_update['title']} - {line}"})
                        expected_yt_dlp_playlist_index += 1

                # Check for download progress
                elif "[download]" in line and "%" in line:
                    # Update last activity time when we see download progress
                    last_activity_time = current_time

        # Wait for process to complete with timeout
        try:
            process_download.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process_download.terminate()
            yield generate_sse_event("error_message", {"message": "Download process timed out. Please try again."})
            return

        stderr_download = process_download.stderr.read() if process_download.stderr else ""

        final_completed_count = sum(1 for s in playlist_items if s["status"] == "completed")
        if final_completed_count < len(playlist_items):
            for song_obj in playlist_items:
                if song_obj["status"] in ["downloading", "queued"]:
                    song_obj["status"] = "failed"
                    yield generate_sse_event("song_status_update", {
                        "id": song_obj["id"],
                        "status": "failed",
                        "title": song_obj["title"]
                    })
            yield generate_sse_event("status_message", {"message": f"Download process completed. {final_completed_count}/{len(playlist_items)} songs downloaded successfully."})
        else:
            yield generate_sse_event("status_message", {"message": "All songs downloaded successfully!"})

    except subprocess.TimeoutExpired:
        yield generate_sse_event("error_message", {"message": "Download process timed out. Please try again."})
        if process_download:
            process_download.terminate()
    except Exception as e:
        yield generate_sse_event("error_message", {"message": f"An unexpected error occurred during download: {str(e)}"})
    finally:
        if process_download:
            try:
                if process_download.stdout:
                    process_download.stdout.close()
                if process_download.stderr:
                    process_download.stderr.close()
                # Ensure process is terminated
                if process_download.poll() is None:
                    process_download.terminate()
                    process_download.wait(timeout=5)
            except Exception:
                pass  # Ignore cleanup errors
        yield generate_sse_event("stream_end", {"message": "Download stream ended."})

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

    return Response(stream_youtube_download(playlist_url, playlist_specific_download_path, sane_playlist_name), mimetype='text/event-stream')

@app.route('/downloads/<playlist_folder>/<filename>')
def downloaded_file(playlist_folder, filename):
    file_path = os.path.join(DOWNLOADS_DIR, playlist_folder)
    return send_from_directory(directory=file_path, path=filename, as_attachment=True)

if __name__ == '__main__':
    if not os.path.exists(DOWNLOADS_DIR):
        os.makedirs(DOWNLOADS_DIR)
    app.run(debug=True, threaded=True) 
