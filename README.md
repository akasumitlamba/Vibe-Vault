# Vibe Vault

## PC app-Coming Before GTA VI


Vibe Vault allows you to download your favorite YouTube Music playlists as MP3 files directly to your PC. It provides a simple web interface for ease of use.

![image](https://github.com/user-attachments/assets/bcb1de00-f47f-46c5-9704-916d05a0030d)

## Features

*   **Web Interface:** Easy-to-use web UI to paste playlist URLs and initiate downloads.
*   **MP3 Downloads:** Saves audio in MP3 format.
*   **Metadata and Thumbnails:** Includes metadata (title, artist, album) and album art for downloaded songs (handled by `yt-dlp`).
*   **Real-time Feedback (Simplified):** Shows a loading indicator during the download process and a list of completed files.
*   **Light/Dark Theme:** Toggle between light and dark themes for user preference.
*   **Interactive Background:** Aesthetically pleasing animated background.


## How to Use

### Without Git Installed (Preferred)
### 📦Download via ZIP File

If you prefer to download the repository as a ZIP file instead of cloning it via Git, follow these steps:

#### 1. **Download the ZIP file**
- Go to the repository: [Vibe-Vault](https://github.com/akasumitlamba/Vibe-Vault)
- Click the **"Code"** button.
- Select **"Download ZIP"** and save the file to your desired location.

#### 2. **Extract the ZIP file**
- Locate the downloaded ZIP file.
- Right-click and select **"Extract Here"** or **"Extract to Vibe-Vault"** (depending on your system).
- Navigate to the extracted folder.

#### 3. **Install dependencies**
- Open a terminal in the extracted folder.
- Ensure Python 3 is installed.
- Run the following command:

```bash
pip install -r requirements.txt
```
#### 4.  **Run the application:**
    
```bash
flask run
```
#### 5.  **Open in your browser:**
Navigate to `http://127.0.0.1:5000` (or the address shown in your terminal).
#### 6.  **Enter Playlist URL:**
Paste the YouTube Music playlist URL into the input field and click "Download Playlist".
#### 7.  **Access Files:**
Downloaded files will be saved in a subfolder named `downloaded_playlist` within a `downloads` directory created in the project's root folder. Links to the files will appear on the page after the download is complete.


### With Git Installed

#### Open Terminal in any folder

#### 1.  **Clone the repository:**
```bash
git clone https://github.com/akasumitlamba/Vibe-Vault.git
cd Vibe-Vault
```
#### 2.  **Install dependencies:**
Make sure you have Python 3 installed.
```bash
pip install -r requirements.txt
```
This will install Flask and yt-dlp. You also need FFmpeg installed and accessible in your system's PATH for `yt-dlp` to convert and embed metadata/thumbnails correctly.
#### 3.  **Run the application:**
```bash
flask run
```
#### 4.  **Open in your browser:**
Navigate to `http://127.0.0.1:5000` (or the address shown in your terminal).
#### 5.  **Enter Playlist URL:**
Paste the YouTube Music playlist URL into the input field and click "Download Playlist".
#### 6.  **Access Files:**
Downloaded files will be saved in a subfolder named `downloaded_playlist` within a `downloads` directory created in the project's root folder. Links to the files will appear on the page after the download is complete.

## License

This project is free to use for personal, non-commercial purposes only. You are not permitted to use this software for any commercial activities.

## Privacy Policy

Vibe Vault runs entirely locally on your computer.
*   **No Data Collection:** It does not collect, store, or transmit any personal data.
*   **No Server Interaction (beyond YouTube):** The application interacts with YouTube to fetch playlist information and download audio content as per your request. It does not send any data to other third-party servers.
*   **Local Storage:** Downloaded files are stored directly on your local machine in a folder you can access. Browser local storage is used only to save your theme preference (light/dark).

## Author

Created by [akasumitlamba](https://github.com/akasumitlamba)
