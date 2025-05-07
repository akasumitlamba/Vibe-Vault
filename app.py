import subprocess
import os
import json
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
        # We need id (video id), title, and playlist_index for mapping
        fetch_command = [
            'yt-dlp',
            '--flat-playlist', # Don't extract info from individual videos yet
            '--print-json',
            '--ignore-errors',
            playlist_url
        ]
        process_fetch = subprocess.Popen(fetch_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout_fetch, stderr_fetch = process_fetch.communicate()

        if process_fetch.returncode != 0:
            err_msg = f"Failed to fetch playlist details. yt-dlp stderr: {stderr_fetch.strip()}"
            yield generate_sse_event("error_message", {"message": err_msg})
            yield generate_sse_event("stream_end", {"message": "Stream ended due to fetch error."})
            return

        for line in stdout_fetch.strip().split('\n'):
            if line:
                try:
                    video_info = json.loads(line)
                    # Ensure we have a unique ID (video_info.get('id')) and a title.
                    # playlist_index from yt-dlp can sometimes be unreliable or absent with --flat-playlist depending on version/source,
                    # so we use our own index for the list, but try to get original if available.
                    item = {
                        "id": video_info.get('id', f"item_{len(playlist_items)}"), # Use video ID if available, else generate one
                        "title": video_info.get('title', 'Untitled Track'),
                        "playlist_index_original": video_info.get('playlist_index'), # Store original if yt-dlp provides it
                        "status": "queued"
                        # Add other fields like 'artist' if reliably available from video_info
                    }
                    playlist_items.append(item)
                except json.JSONDecodeError:
                    yield generate_sse_event("info_message", {"message": f"Skipping malformed JSON line from playlist info: {line[:100]}"})
        
        if not playlist_items:
            yield generate_sse_event("error_message", {"message": "No items found in the playlist or failed to parse details."})
            yield generate_sse_event("stream_end", {"message": "Stream ended, no items."})
            return

        yield generate_sse_event("initial_song_list", {"songs": playlist_items})
        yield generate_sse_event("overall_progress", {"downloaded_count": 0, "total_count": len(playlist_items), "percentage": 0})
        yield generate_sse_event("status_message", {"message": "Playlist fetched. Starting downloads..."})

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
        # Output template is crucial for matching later. Using playlist_index if available, or title.
        # We need a way to reliably map the downloaded file back to the initial song list.
        # yt-dlp's %(playlist_index)s in the output template for the *download* command refers to its internal processing order for that command run,
        # which should align with the order of items if we pass the whole playlist URL.
        '-o', os.path.join(playlist_specific_download_path, '%(playlist_index)s - %(title)s.%(ext)s'),
        '--yes-playlist',
        '--ignore-errors', # Continue on individual file errors
        '--newline',
        playlist_url # Download the whole playlist again
    ]

    process_download = None
    songs_processed_count = 0
    # Create a mapping from `playlist_index_original` (if available and unique) or generated ID to our song object for quick updates.
    # For simplicity now, we'll rely on title and the order, but playlist_index in output template is better.
    # When yt-dlp processes the playlist for download, its internal playlist_index for the -o template should be 1-based sequential.

    try:
        process_download = subprocess.Popen(download_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
        
        # Keep track of which original playlist_index (1-based from yt-dlp for -o template) we expect next
        expected_yt_dlp_playlist_index = 1 

        if process_download.stdout:
            for line in iter(process_download.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                
                # Attempt to parse title and playlist_index from output if possible
                # This is the tricky part: correlating yt-dlp's download-time output to our initial list.
                # We will primarily rely on [ExtractAudio] for confirmation of an MP3 for a given title/index.

                current_song_to_update = None
                song_index_in_our_list = expected_yt_dlp_playlist_index - 1 # Our list is 0-indexed

                if 0 <= song_index_in_our_list < len(playlist_items):
                    current_song_to_update = playlist_items[song_index_in_our_list]
                
                if current_song_to_update and current_song_to_update["status"] == "queued":
                     yield generate_sse_event("song_status_update", {"id": current_song_to_update["id"], "status": "downloading", "title": current_song_to_update["title"]})
                     current_song_to_update["status"] = "downloading" # Update our internal state
                     yield generate_sse_event("status_message", {"message": f"Downloading: {current_song_to_update['title']}"})

                if "[ExtractAudio] Destination:" in line and line.endswith(".mp3"):
                    # This means an MP3 was successfully created.
                    # We need to find which song this belongs to.
                    # The filename format is '%(playlist_index)s - %(title)s.%(ext)s'
                    try:
                        filename_part = line.split("Destination:",1)[-1].strip()
                        base_filename = os.path.basename(filename_part)
                        yield generate_sse_event("info_message", {"message": f"[Debug] Detected MP3: {base_filename}"}) # Log filename
                        
                        parts = base_filename.split(' - ', 1)
                        dl_playlist_idx_str = parts[0]
                        dl_playlist_idx = int(dl_playlist_idx_str) # Can fail if not integer
                        yield generate_sse_event("info_message", {"message": f"[Debug] Parsed Index: {dl_playlist_idx}"}) # Log index

                        target_song_index = dl_playlist_idx - 1 # yt-dlp -o %(playlist_index)s is 1-based

                        if 0 <= target_song_index < len(playlist_items):
                            song_obj = playlist_items[target_song_index]
                            yield generate_sse_event("info_message", {"message": f"[Debug] Matched Song: ID={song_obj['id']}, Title={song_obj['title']}"}) # Log matched song

                            if song_obj["status"] != "completed": 
                                song_obj["status"] = "completed"
                                songs_processed_count += 1
                                
                                update_event_data = {"id": song_obj["id"], "status": "completed", "title": song_obj["title"]}
                                yield generate_sse_event("info_message", {"message": f"[Debug] Sending song_status_update: {json.dumps(update_event_data)}"}) # Log event data
                                yield generate_sse_event("song_status_update", update_event_data)
                                
                                progress_event_data = {"downloaded_count": songs_processed_count, "total_count": len(playlist_items), "percentage": round((songs_processed_count / len(playlist_items)) * 100)}
                                yield generate_sse_event("info_message", {"message": f"[Debug] Sending overall_progress: {json.dumps(progress_event_data)}"}) # Log event data
                                yield generate_sse_event("overall_progress", progress_event_data)
                                
                                expected_yt_dlp_playlist_index = dl_playlist_idx + 1 
                        else:
                             yield generate_sse_event("info_message", {"message": f"[Debug] Parsed index {dl_playlist_idx} out of range for playlist_items (len {len(playlist_items)})"})
                             yield generate_sse_event("error_message", {"message": f"Could not map completed file to song list: {base_filename}"}) 

                    except ValueError:
                         yield generate_sse_event("info_message", {"message": f"[Debug] ValueError parsing index from: {dl_playlist_idx_str}"}) 
                         yield generate_sse_event("error_message", {"message": f"Failed to parse index from filename: {base_filename}"}) 
                    except Exception as parse_ex:
                        yield generate_sse_event("info_message", {"message": f"[Debug] Exception parsing filename: {parse_ex}"}) 
                        yield generate_sse_event("error_message", {"message": f"Could not parse filename from [ExtractAudio] line: {line}"})
                
                elif "ERROR:" in line.upper(): # General errors from yt-dlp
                    # Try to associate error with the current song if possible
                    error_associated = False
                    if current_song_to_update and current_song_to_update["status"] == "downloading":
                        # Assume error is for the song we thought was downloading
                        current_song_to_update["status"] = "failed"
                        songs_processed_count +=1 # Count as processed (attempted)
                        yield generate_sse_event("song_status_update", {"id": current_song_to_update["id"], "status": "failed", "title": current_song_to_update["title"]})
                        yield generate_sse_event("error_message", {"message": f"Failed: {current_song_to_update['title']} - {line}"})
                        yield generate_sse_event("overall_progress", {"downloaded_count": songs_processed_count, "total_count": len(playlist_items), "percentage": round((songs_processed_count / len(playlist_items)) * 100)})
                        expected_yt_dlp_playlist_index += 1 # Move to next song expectation
                        error_associated = True
                    
                    # Send a general error too, or if it couldn't be associated
                    if not error_associated:
                        yield generate_sse_event("error_message", {"message": line})

                # General info messages for verbosity, like in previous iterations
                elif line.startswith("[") and "]" in line:
                    if not ("%" in line and "ETA" in line and line.startswith("[download]")):
                        yield generate_sse_event("info_message", {"message": line})

        process_download.wait()
        stderr_download = process_download.stderr.read() if process_download.stderr else ""

        # After loop, mark any remaining 'downloading' or 'queued' as 'failed' if not all completed
        final_completed_count = sum(1 for s in playlist_items if s["status"] == "completed")
        if final_completed_count < len(playlist_items):
            for song_obj in playlist_items:
                if song_obj["status"] == "downloading" or song_obj["status"] == "queued":
                    song_obj["status"] = "failed"
                    yield generate_sse_event("song_status_update", {"id": song_obj["id"], "status": "failed", "title": song_obj["title"]})
            yield generate_sse_event("status_message", {"message": f"Download process completed. {final_completed_count}/{len(playlist_items)} songs downloaded."})
        else:
            yield generate_sse_event("status_message", {"message": "All songs downloaded successfully!"})
        
        if process_download.returncode != 0 and stderr_download:
            if not any(err_line.startswith("ERROR:") for err_line in stderr_download.splitlines()):
                 yield generate_sse_event("error_message", {"message": f"yt-dlp download process finished with errors: {stderr_download.strip()}"})

    except FileNotFoundError:
        yield generate_sse_event("error_message", {"message": "Error: yt-dlp command not found."}) 
    except Exception as e:
        yield generate_sse_event("error_message", {"message": f"An unexpected error occurred during download: {str(e)}"}) 
    finally:
        if process_download and process_download.stdout:
            process_download.stdout.close()
        if process_download and process_download.stderr:
            process_download.stderr.close()
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