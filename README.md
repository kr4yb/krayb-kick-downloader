# Krayb's Kick Downloader

Desktop app for downloading Kick VODs and chat logs with a clean PyQt interface.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Why This Project Exists

This tool was built to make archiving Kick streams simple: paste a username, pick VODs, and download videos or chat logs without using command-line tools.

## Features

- Fetch public VODs from a Kick channel
- Select one or many VODs at once
- Download VODs via `yt-dlp`
- Export chat logs to JSON (with relative message offsets)
- Track progress and status in real time

## Requirements

- Python 3.8+
- Windows 10/11 (macOS/Linux may work but are untested)
- `yt-dlp` available in your system path (or `yt-dlp.exe` one level above this project)
- FFmpeg binary at `ffmpeg/ffmpeg.exe` (for muxing/output formatting)

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

## How To Use

1. Enter a Kick username and click **Fetch**.
2. Click one or more VOD cards to select them.
3. Use **Download VOD** for video files or **Download Chat** for JSON chat exports.
4. Change the destination folder with the browse button if needed.

Default output folder: `downloads/`

## Dependencies

- `PyQt6` - desktop GUI
- `curl_cffi` - Kick API requests with browser impersonation
- `yt-dlp` - VOD downloading
- `requests` - thumbnail and avatar fetches
- `orjson` (optional) - faster JSON serialization

## Troubleshooting

- **No VODs found**: check username spelling or verify the channel has public VODs.
- **Download failed**: retry after a few minutes (temporary rate-limits happen).
- **App does not launch**: reinstall dependencies from `requirements.txt`.

## Legal

Personal use only. Respect Kick's Terms of Service and creator rights.

## License

MIT
