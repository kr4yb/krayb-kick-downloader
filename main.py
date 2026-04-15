import sys
import os
import json
import re
import threading
import subprocess
import shutil
import urllib.parse
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Optional: orjson for faster JSON serialization
try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False
    orjson = None  # type: ignore

# Optional: curl_cffi for browser impersonation (required for Kick API)
try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False
    curl_requests = None  # type: ignore

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QProgressBar,
    QScrollArea, QFrame, QFileDialog, QMessageBox
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QByteArray, QPropertyAnimation,
    QEasingCurve, QRect, QPoint
)
from PyQt6.QtGui import QFont, QPixmap, QColor, QPainter, QBrush, QPen, QPainterPath

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


class KickTheme:
    PRIMARY = "#13ec13"
    PRIMARY_DARK = "#0fb80f"
    PRIMARY_LIGHT = "#4aff4a"
    BG_DARK = "#102210"
    BG_CARD = "#1a2e1a"
    BG_INPUT = "#1a2e1a"
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#9db99d"
    TEXT_MUTED = "#6b8b6b"
    BORDER = "rgba(255, 255, 255, 0.1)"
    BORDER_HOVER = "rgba(255, 255, 255, 0.2)"
    RED = "#ef4444"
    SHADOW = "rgba(0, 0, 0, 0.3)"


def _seconds_to_duration(seconds: Any) -> str:
    if seconds is None:
        return "?"
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds) if seconds else "?"
    if s < 0:
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _format_views(views: int) -> str:
    """Format view count (e.g., 1.2k, 45.3k)"""
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M"
    elif views >= 1_000:
        return f"{views / 1_000:.1f}k"
    return str(views)


def _format_date(date_str: str) -> str:
    """Format date to relative time"""
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        diff = now - dt
        
        if diff.days == 0:
            return "Today"
        elif diff.days == 1:
            return "Yesterday"
        elif diff.days < 7:
            return f"{diff.days} days ago"
        elif diff.days < 30:
            weeks = diff.days // 7
            return f"{weeks} week{'s' if weeks > 1 else ''} ago"
        else:
            months = diff.days // 30
            return f"{months} month{'s' if months > 1 else ''} ago"
    except Exception:
        return "Unknown"


class VideoInfo:
    def __init__(self, url: str, title: str, duration: str, thumbnail: Optional[str] = None,
                 views: int = 0, created_at: str = "", video_id: str = ""):
        self.url = url
        self.title = title
        self.duration = duration
        self.thumbnail = thumbnail
        self.views = views
        self.views_formatted = _format_views(views)
        self.created_at = created_at
        self.date_formatted = _format_date(created_at) if created_at else "Unknown"
        self.video_id = video_id or url.split("/")[-1]
        self.selected = False
        self.downloading = False
        self.progress = 0
        self.speed = ""
        self.eta = ""


class ChannelInfo:
    def __init__(self, username: str, display_name: str = "", avatar: str = "",
                 followers: int = 0, is_live: bool = False, verified: bool = False):
        self.username = username
        self.display_name = display_name or username
        self.avatar = avatar
        self.followers = followers
        self.followers_formatted = _format_views(followers)
        self.is_live = is_live
        self.verified = verified


class ScraperThread(QThread):
    videos_loaded = pyqtSignal(object, list)  # channel_info, videos
    error_occurred = pyqtSignal(str)
    progress_updated = pyqtSignal(str)
    
    def __init__(self, username: str):
        super().__init__()
        self.username = username
        
    def run(self):
        try:
            self.progress_updated.emit("Fetching channel info...")
            channel_info, videos = self.fetch_channel_data(self.username)
            self.videos_loaded.emit(channel_info, videos)
        except Exception as e:
            self.error_occurred.emit(str(e))
    
    def fetch_channel_data(self, username: str):
        """Fetch channel info and videos using curl_cffi"""
        if not _HAS_CURL_CFFI:
            raise Exception("curl_cffi not available. Please install it: pip install curl_cffi>=0.5.10,<0.14")
        
        api_url = f"https://kick.com/api/v1/channels/{username}"
        self.progress_updated.emit("Connecting to Kick API...")
        
        resp = curl_requests.get(  # type: ignore[union-attr]
            api_url,
            impersonate="chrome131",
            headers={
                "Accept": "application/json",
                "Referer": f"https://kick.com/{username}",
            },
            timeout=30,
        )
        
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch channel (HTTP {resp.status_code})")
        
        content_type = resp.headers.get('content-type', '')
        if 'html' in content_type:
            raise Exception("Kick returned HTML instead of JSON - channel may not exist")
        
        data = resp.json()
        
        # Parse channel info
        user = data.get("user", {})
        channel_info = ChannelInfo(
            username=data.get("slug") or username,
            display_name=user.get("username") or username,
            avatar=user.get("profile_pic") or "",
            followers=data.get("followers_count", 0),
            is_live=data.get("livestream") is not None,
            verified=data.get("verified", False),
        )
        
        # Parse videos
        self.progress_updated.emit("Parsing videos...")
        videos = self._parse_videos(data, username)
        
        return channel_info, videos
    
    def _parse_videos(self, data: dict, username: str) -> List[VideoInfo]:
        """Parse videos from API response"""
        items = data.get("previous_livestreams", [])
        videos = []
        
        for item in items:
            if not isinstance(item, dict):
                continue
            
            video_obj = item.get("video") or {}
            slug = video_obj.get("uuid") or item.get("slug") or item.get("id")
            if not slug:
                continue
            
            full_url = f"https://kick.com/{username}/videos/{slug}"
            title = item.get("session_title") or item.get("title") or "Untitled"
            
            raw_duration = item.get("duration")
            if raw_duration is not None:
                duration = _seconds_to_duration(raw_duration / 1000)
            else:
                duration = "?"
            
            thumb_obj = item.get("thumbnail")
            if isinstance(thumb_obj, dict):
                thumbnail = thumb_obj.get("src") or thumb_obj.get("url")
            elif isinstance(thumb_obj, str):
                thumbnail = thumb_obj
            else:
                thumbnail = None
            
            if thumbnail and thumbnail.startswith("//"):
                thumbnail = "https:" + thumbnail
            elif thumbnail and thumbnail.startswith("/"):
                thumbnail = "https://kick.com" + thumbnail
            
            views = item.get("viewer_count") or item.get("views") or 0
            created_at = item.get("created_at") or item.get("start_time") or ""
            
            videos.append(VideoInfo(
                url=full_url,
                title=title,
                duration=duration,
                thumbnail=thumbnail,
                views=views,
                created_at=created_at,
                video_id=slug
            ))
            
            self.progress_updated.emit(f"Found {len(videos)} videos...")
        
        return videos


# ============== Download Thread ==============
class DownloadThread(QThread):
    progress_updated = pyqtSignal(str, int, str, str)  # video_id, progress, speed, eta
    download_completed = pyqtSignal(str)  # video_id
    download_error = pyqtSignal(str, str)  # video_id, error
    all_completed = pyqtSignal()
    
    def __init__(self, videos: List[VideoInfo], download_path: str):
        super().__init__()
        self.videos = videos
        self.download_path = download_path
        self.cancelled = False
        
    def run(self):
        try:
            os.makedirs(self.download_path, exist_ok=True)
            
            for video in self.videos:
                if self.cancelled:
                    break
                
                self._download_video(video)
            
            if not self.cancelled:
                self.all_completed.emit()
                
        except Exception as e:
            logger.error(f"Download error: {e}")
    
    def _download_video(self, video: VideoInfo):
        """Download a single video using yt-dlp"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            ytdlp_local = os.path.join(current_dir, "..", "yt-dlp.exe")
            ytdlp_system = shutil.which("yt-dlp")
            
            if os.path.isfile(ytdlp_local):
                ytdlp_path = ytdlp_local
            elif ytdlp_system:
                ytdlp_path = ytdlp_system
            else:
                ytdlp_path = "yt-dlp"
            
            ffmpeg_path = os.path.join(current_dir, "ffmpeg", "ffmpeg.exe")
            
            command = [
                ytdlp_path,
                "-P", self.download_path,
                "--impersonate", "chrome-131",
                "--ffmpeg-location", ffmpeg_path,
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "5",
                video.url,
            ]
            
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            
            for line in iter(process.stdout.readline, ""):  # type: ignore[union-attr]
                if self.cancelled:
                    process.kill()
                    break
                
                if "[download]" in line and "%" in line:
                    match = re.search(r"\[download\]\s+([\d\.]+)%", line)
                    if match:
                        percent = int(float(match.group(1)))
                        
                        speed = ""
                        speed_match = re.search(r"at\s+([\d\.]+\s*[KMGT]iB/s)", line)
                        if speed_match:
                            speed = speed_match.group(1)
                        
                        eta = ""
                        eta_match = re.search(r"ETA\s+(\d+:\d+(?::\d+)?)", line)
                        if eta_match:
                            eta = eta_match.group(1)
                        
                        self.progress_updated.emit(video.video_id, percent, speed, eta)
            
            process.stdout.close()  # type: ignore[union-attr]
            retcode = process.wait()
            
            if retcode == 0:
                self.download_completed.emit(video.video_id)
            else:
                self.download_error.emit(video.video_id, f"Download failed (code {retcode})")
                
        except Exception as e:
            self.download_error.emit(video.video_id, str(e))
    
    def cancel(self):
        self.cancelled = True


class ChatDownloadThread(QThread):
    progress_updated = pyqtSignal(str, int, str)  # video_id, progress%, status_text
    download_completed = pyqtSignal(str, str)  # video_id, file_path
    download_error = pyqtSignal(str, str)  # video_id, error
    all_completed = pyqtSignal()
    
    def __init__(self, videos: List[VideoInfo], download_path: str, concurrent: int = 100):
        super().__init__()
        self.videos = videos
        self.download_path = download_path
        self.concurrent = concurrent
        self.cancelled = False
        # Create a session for connection pooling
        self._session = curl_requests.Session() if _HAS_CURL_CFFI else None  # type: ignore[union-attr]
    
    def run(self):
        try:
            os.makedirs(self.download_path, exist_ok=True)
            
            for video in self.videos:
                if self.cancelled:
                    break
                self._download_chat(video)
            
            if not self.cancelled:
                self.all_completed.emit()
        except Exception as e:
            logger.error(f"Chat download error: {e}")
    
    def _download_chat(self, video: VideoInfo):
        """Download chat for a single video"""
        try:
            self.progress_updated.emit(video.video_id, 0, "Fetching video metadata...")
            
            # Get video metadata
            if not _HAS_CURL_CFFI:
                self.download_error.emit(video.video_id, "curl_cffi not available")
                return
            
            video_uuid = video.video_id
            url = f"https://kick.com/api/v1/video/{video_uuid}"
            resp = curl_requests.get(url, impersonate="chrome131", timeout=30)  # type: ignore[union-attr]
            
            if resp.status_code != 200:
                self.download_error.emit(video.video_id, f"Failed to fetch video metadata: {resp.status_code}")
                return
            
            data = resp.json()
            livestream = data.get("livestream", {})
            channel_id = livestream.get("channel_id")
            start_time_str = livestream.get("start_time")
            duration_ms = livestream.get("duration")
            
            if not all([channel_id, start_time_str, duration_ms]):
                self.download_error.emit(video.video_id, "Missing livestream metadata")
                return
            
            duration_seconds = duration_ms / 1000
            
            self.progress_updated.emit(video.video_id, 5, f"Downloading chat ({int(duration_seconds/60)} min stream)...")
            
            # Calculate time slices (every 5 seconds - API returns ~5s window per request)
            start_time = datetime.fromisoformat(start_time_str.replace('+00:00', '+00:00').replace('Z', '+00:00'))
            
            # Use 5-second intervals to capture all messages
            slice_interval = 5
            time_slices = []
            for s in range(0, int(duration_seconds), slice_interval):
                slice_time = start_time + timedelta(seconds=s)
                time_slices.append(slice_time.isoformat())
            
            total_slices = len(time_slices)
            all_messages = []
            errors = 0
            completed = 0
            lock = threading.Lock()
            session = self._session  # Use session for connection pooling
            
            def fetch_slice(iso_time):
                nonlocal errors, completed
                if self.cancelled:
                    return []
                
                try:
                    slice_url = f"https://kick.com/api/v2/channels/{channel_id}/messages?start_time={urllib.parse.quote(iso_time)}"
                    # Use session for HTTP/2 connection reuse
                    r = session.get(slice_url, impersonate="chrome131", timeout=15) if session else curl_requests.get(slice_url, impersonate="chrome131", timeout=15)  # type: ignore[union-attr]
                    
                    if r.status_code != 200:
                        with lock:
                            errors += 1
                        return []
                    
                    json_data = r.json()
                    messages = json_data.get("data", {}).get("messages", [])
                    return messages
                except Exception:
                    with lock:
                        errors += 1
                    return []
                finally:
                    with lock:
                        completed += 1
                        if completed % 50 == 0 or completed == total_slices:
                            progress = 5 + int((completed / total_slices) * 90)
                            self.progress_updated.emit(
                                video.video_id, 
                                progress, 
                                f"Fetching messages... {completed}/{total_slices}"
                            )
            
            # Run concurrent requests
            with ThreadPoolExecutor(max_workers=self.concurrent) as executor:
                futures = {executor.submit(fetch_slice, ts): ts for ts in time_slices}
                
                for future in as_completed(futures):
                    if self.cancelled:
                        executor.shutdown(wait=False, cancel_futures=True)
                        return
                    
                    messages = future.result()
                    if messages:
                        all_messages.extend(messages)
            
            self.progress_updated.emit(video.video_id, 95, "Processing messages...")
            
            # Deduplicate and sort messages
            seen_ids = set()
            unique_messages = []
            for msg in all_messages:
                msg_id = msg.get("id")
                if msg_id and msg_id not in seen_ids:
                    seen_ids.add(msg_id)
                    unique_messages.append(msg)
            
            # Sort by created_at
            unique_messages.sort(key=lambda m: m.get("created_at", ""))
            
            # Add relative time offset
            for msg in unique_messages:
                try:
                    msg_time = datetime.fromisoformat(msg["created_at"].replace('Z', '+00:00'))
                    offset_seconds = (msg_time - start_time).total_seconds()
                    msg["offset_seconds"] = max(0, offset_seconds)
                except Exception:
                    msg["offset_seconds"] = 0
            
            self.progress_updated.emit(video.video_id, 98, "Saving JSON...")
            
            # Save to JSON
            safe_title = "".join(c for c in video.title[:50] if c.isalnum() or c in " -_").strip()
            filename = f"{safe_title}_chat.json"
            filepath = os.path.join(self.download_path, filename)
            
            output = {
                "video_id": video_uuid,
                "title": video.title,
                "channel_id": channel_id,
                "start_time": start_time_str,
                "duration_seconds": duration_seconds,
                "message_count": len(unique_messages),
                "errors": errors,
                "messages": unique_messages
            }
            
            # Use orjson if available (10-20x faster), otherwise use stdlib json without indent
            if _HAS_ORJSON:
                with open(filepath, "wb") as f:
                    f.write(orjson.dumps(output, option=orjson.OPT_INDENT_2))  # type: ignore[union-attr]
            else:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(output, f, ensure_ascii=False)
            
            self.progress_updated.emit(video.video_id, 100, f"Done! {len(unique_messages)} messages")
            self.download_completed.emit(video.video_id, filepath)
            
        except Exception as e:
            logger.error(f"Chat download error for {video.video_id}: {e}")
            self.download_error.emit(video.video_id, str(e))
    
    def cancel(self):
        self.cancelled = True


class VideoCard(QFrame):
    
    clicked = pyqtSignal(object)  # Emits VideoInfo
    
    def __init__(self, video: VideoInfo, parent=None):
        super().__init__(parent)
        self.video = video
        self.selected = False
        self.downloading = False
        self.progress = 0
        self.speed = ""
        self.eta = ""
        self._thumbnail_pixmap = None
        
        self.setup_ui()
        self.load_thumbnail()
    
    def setup_ui(self):
        self.setFixedSize(320, 280)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_style()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Thumbnail container
        self.thumbnail_container = QFrame()
        self.thumbnail_container.setObjectName("thumbContainer")
        self.thumbnail_container.setFixedHeight(180)
        self.thumbnail_container.setStyleSheet("""
            QFrame#thumbContainer {
                background-color: #0d1a0d;
                border-radius: 0px;
            }
        """)
        
        thumb_layout = QVBoxLayout(self.thumbnail_container)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        
        # Thumbnail label
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setStyleSheet("background-color: transparent;")
        thumb_layout.addWidget(self.thumbnail_label)
        
        layout.addWidget(self.thumbnail_container)
        
        # Info container
        info_container = QFrame()
        info_container.setObjectName("infoContainer")
        info_container.setStyleSheet("""
            QFrame#infoContainer {
                background-color: #1a2e1a;
                border-radius: 0px;
            }
        """)
        
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(6)
        
        # Title
        self.title_label = QLabel(self.video.title)
        self.title_label.setWordWrap(True)
        self.title_label.setMaximumHeight(36)
        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_PRIMARY};
                font-size: 12px;
                font-weight: bold;
                background-color: transparent;
            }}
        """)
        info_layout.addWidget(self.title_label)
        
        # Meta info row 1: Duration and Views
        meta_layout1 = QHBoxLayout()
        meta_layout1.setSpacing(16)
        
        # Duration
        duration_label = QLabel(f"⏱ {self.video.duration}")
        duration_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_PRIMARY};
                font-size: 11px;
                font-weight: bold;
                background-color: transparent;
            }}
        """)
        meta_layout1.addWidget(duration_label)
        
        # Views
        views_label = QLabel(f"👁 {self.video.views_formatted}")
        views_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_SECONDARY};
                font-size: 11px;
                background-color: transparent;
            }}
        """)
        meta_layout1.addWidget(views_label)
        
        meta_layout1.addStretch()
        
        # Date
        date_label = QLabel(f"{self.video.date_formatted}")
        date_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_MUTED};
                font-size: 10px;
                background-color: transparent;
            }}
        """)
        meta_layout1.addWidget(date_label)
        
        info_layout.addLayout(meta_layout1)
        
        layout.addWidget(info_container)
        
        # Progress overlay (positioned absolutely over the info section)
        self.progress_container = QFrame(self)
        self.progress_container.setVisible(False)
        self.progress_container.setGeometry(0, 180, 320, 100)  # Below thumbnail
        self.progress_container.setStyleSheet("""
            QFrame {
                background-color: rgba(16, 34, 16, 0.95);
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }
        """)
        
        progress_layout = QVBoxLayout(self.progress_container)
        progress_layout.setContentsMargins(12, 12, 12, 12)
        progress_layout.setSpacing(8)
        
        # Status label
        self.progress_status_label = QLabel("Downloading...")
        self.progress_status_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.PRIMARY};
                font-size: 11px;
                font-weight: bold;
                background: transparent;
            }}
        """)
        progress_layout.addWidget(self.progress_status_label)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(255, 255, 255, 0.1);
                border-radius: 3px;
                border: none;
            }}
            QProgressBar::chunk {{
                background-color: {KickTheme.PRIMARY};
                border-radius: 3px;
            }}
        """)
        progress_layout.addWidget(self.progress_bar)
        
        # Speed and ETA row
        speed_layout = QHBoxLayout()
        speed_layout.setSpacing(8)
        
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet(f"color: {KickTheme.TEXT_PRIMARY}; font-size: 11px; font-weight: bold; background: transparent;")
        speed_layout.addWidget(self.speed_label)
        
        speed_layout.addStretch()
        
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet(f"color: {KickTheme.TEXT_SECONDARY}; font-size: 11px; background: transparent;")
        speed_layout.addWidget(self.eta_label)
        
        progress_layout.addLayout(speed_layout)
        progress_layout.addStretch()
    
    def load_thumbnail(self):
        """Load thumbnail asynchronously"""
        if self.video.thumbnail:
            thread = threading.Thread(target=self._fetch_thumbnail, daemon=True)
            thread.start()
    
    def _fetch_thumbnail(self):
        """Fetch thumbnail in background"""
        try:
            if not self.video.thumbnail:
                return
            response = requests.get(self.video.thumbnail, timeout=10)
            if response.status_code == 200:
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(response.content))
                self._thumbnail_pixmap = pixmap.scaled(
                    320, 180,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                # Update UI in main thread
                QTimer.singleShot(0, self._update_thumbnail)
        except Exception as e:
            logger.debug(f"Failed to load thumbnail: {e}")
    
    def _update_thumbnail(self):
        """Update thumbnail in main thread"""
        if self._thumbnail_pixmap:
            self.thumbnail_label.setPixmap(self._thumbnail_pixmap)
    
    def update_style(self):
        """Update card style based on state"""
        if self.selected:
            # Strong green border for selected cards
            self.setStyleSheet(f"""
                VideoCard {{
                    background-color: {KickTheme.BG_CARD};
                    border: 3px solid {KickTheme.PRIMARY};
                    border-radius: 12px;
                }}
                VideoCard:hover {{
                    border: 3px solid {KickTheme.PRIMARY_LIGHT};
                }}
                QFrame#infoContainer {{
                    background-color: #1a2e1a;
                }}
                QFrame#thumbContainer {{
                    background-color: #0d1a0d;
                }}
                QLabel {{
                    background-color: transparent;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                VideoCard {{
                    background-color: {KickTheme.BG_CARD};
                    border: 1px solid {KickTheme.BORDER};
                    border-radius: 12px;
                }}
                VideoCard:hover {{
                    border: 1px solid {KickTheme.BORDER_HOVER};
                }}
                QFrame#infoContainer {{
                    background-color: #1a2e1a;
                }}
                QFrame#thumbContainer {{
                    background-color: #0d1a0d;
                }}
                QLabel {{
                    background-color: transparent;
                }}
            """)
    
    def set_selected(self, selected: bool):
        """Set selection state"""
        self.selected = selected
        self.video.selected = selected
        self.update_style()
        self.update()
    
    def set_downloading(self, downloading: bool):
        """Set downloading state"""
        self.downloading = downloading
        self.video.downloading = downloading
        self.progress_container.setVisible(downloading)
        if downloading:
            self.progress_status_label.setText("Downloading...")
            self.progress_bar.setValue(0)
        self.update()
    
    def set_progress(self, progress: int, speed: str = "", eta: str = ""):
        """Update download progress"""
        self.progress = progress
        self.speed = speed
        self.eta = eta
        self.progress_bar.setValue(progress)
        self.progress_status_label.setText(f"Downloading... {progress}%")
        self.speed_label.setText(speed)
        self.eta_label.setText(f"ETA: {eta}" if eta else "")
    
    def mousePressEvent(self, event):
        """Handle click to toggle selection"""
        self.set_selected(not self.selected)
        self.clicked.emit(self.video)
        super().mousePressEvent(event)
    
    def paintEvent(self, event):
        """Custom paint for overlay effects"""
        super().paintEvent(event)
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw selection checkbox
        checkbox_rect = QRect(self.width() - 35, 10, 24, 24)
        
        if self.selected:
            # Filled green circle with check
            painter.setBrush(QBrush(QColor(KickTheme.PRIMARY)))
            painter.setPen(QPen(QColor(KickTheme.PRIMARY), 2))
            painter.drawEllipse(checkbox_rect)
            
            # Draw checkmark
            painter.setPen(QPen(QColor("#000000"), 2))
            painter.drawLine(
                checkbox_rect.x() + 6, checkbox_rect.y() + 12,
                checkbox_rect.x() + 10, checkbox_rect.y() + 16
            )
            painter.drawLine(
                checkbox_rect.x() + 10, checkbox_rect.y() + 16,
                checkbox_rect.x() + 18, checkbox_rect.y() + 8
            )
        else:
            # Empty circle
            painter.setBrush(QBrush(QColor(0, 0, 0, 100)))
            painter.setPen(QPen(QColor(255, 255, 255, 50), 1))
            painter.drawEllipse(checkbox_rect)
        
        # Draw duration badge
        if self.video.duration:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 200)))
            
            font = painter.font()
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            
            duration_text = self.video.duration
            text_width = painter.fontMetrics().horizontalAdvance(duration_text) + 12
            duration_rect = QRect(8, 155, text_width, 20)
            
            painter.drawRoundedRect(duration_rect, 4, 4)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(duration_rect, Qt.AlignmentFlag.AlignCenter, duration_text)
        
        # Draw "PAST" badge
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(71, 85, 105)))
        past_rect = QRect(8 + text_width + 4, 155, 40, 20)
        painter.drawRoundedRect(past_rect, 4, 4)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(past_rect, Qt.AlignmentFlag.AlignCenter, "PAST")
        
        # Draw download overlay if downloading
        if self.downloading:
            # Semi-transparent overlay
            painter.setBrush(QBrush(QColor(0, 0, 0, 150)))
            painter.setPen(Qt.PenStyle.NoPen)
            
            path = QPainterPath()
            path.addRoundedRect(0, 0, self.width(), 180, 12, 12)
            painter.drawPath(path)
            
            # Draw circular progress
            center = QPoint(self.width() // 2, 90)
            radius = 32
            
            # Background circle
            painter.setPen(QPen(QColor(255, 255, 255, 30), 4))
            painter.drawEllipse(center, radius, radius)
            
            # Progress arc
            painter.setPen(QPen(QColor(KickTheme.PRIMARY), 4))
            start_angle = 90 * 16  # Start from top
            span_angle = -int(self.progress * 3.6 * 16)  # Convert percentage to degrees
            painter.drawArc(
                center.x() - radius, center.y() - radius,
                radius * 2, radius * 2,
                start_angle, span_angle
            )
            
            # Progress text
            painter.setPen(QPen(QColor("#ffffff")))
            font = painter.font()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRect(center.x() - 20, center.y() - 10, 40, 20),
                Qt.AlignmentFlag.AlignCenter,
                f"{self.progress}%"
            )
            
            # Downloading badge
            painter.setBrush(QBrush(QColor(KickTheme.PRIMARY)))
            painter.setPen(Qt.PenStyle.NoPen)
            badge_rect = QRect(self.width() - 35, 10, 24, 24)
            painter.drawEllipse(badge_rect)


class ChannelHeader(QFrame):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.channel_info = None
        self.avatar_pixmap = None
        self.setup_ui()
        self.hide()
    
    def setup_ui(self):
        self.setStyleSheet("""
            QFrame {
                background: transparent;
            }
        """)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(20)
        
        # Avatar
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(96, 96)
        self.avatar_label.setStyleSheet(f"""
            QLabel {{
                background-color: {KickTheme.BG_CARD};
                border: 4px solid rgba(19, 236, 19, 0.2);
                border-radius: 48px;
            }}
        """)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar_label)
        
        # Info container
        info_container = QWidget()
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)
        
        # Username row
        name_layout = QHBoxLayout()
        name_layout.setSpacing(8)
        
        self.name_label = QLabel()
        self.name_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_PRIMARY};
                font-size: 24px;
                font-weight: bold;
                background: transparent;
            }}
        """)
        name_layout.addWidget(self.name_label)
        
        self.verified_label = QLabel("✓")
        self.verified_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.PRIMARY};
                font-size: 18px;
                font-weight: bold;
                background: transparent;
            }}
        """)
        self.verified_label.hide()
        name_layout.addWidget(self.verified_label)
        
        name_layout.addStretch()
        info_layout.addLayout(name_layout)
        
        # Followers (hidden)
        self.followers_label = QLabel()
        self.followers_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_SECONDARY};
                font-size: 14px;
                background: transparent;
            }}
        """)
        self.followers_label.hide()  # Hidden per user request
        info_layout.addWidget(self.followers_label)
        
        # Live indicator
        self.live_container = QWidget()
        live_layout = QHBoxLayout(self.live_container)
        live_layout.setContentsMargins(0, 4, 0, 0)
        live_layout.setSpacing(8)
        
        self.live_dot = QLabel("●")
        self.live_dot.setStyleSheet(f"color: {KickTheme.RED}; font-size: 10px;")
        live_layout.addWidget(self.live_dot)
        
        self.live_text = QLabel("LIVE NOW")
        self.live_text.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.RED};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }}
        """)
        live_layout.addWidget(self.live_text)
        live_layout.addStretch()
        
        self.live_container.hide()
        info_layout.addWidget(self.live_container)
        
        info_layout.addStretch()
        layout.addWidget(info_container)
        
        layout.addStretch()
        
        # Action buttons
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setSpacing(8)
        
        self.select_all_btn = QPushButton("☑ Select All")
        self.select_all_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(19, 236, 19, 0.1);
                color: {KickTheme.PRIMARY};
                border: 1px solid rgba(19, 236, 19, 0.3);
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: rgba(19, 236, 19, 0.2);
            }}
        """)
        btn_layout.addWidget(self.select_all_btn)
        
        layout.addWidget(btn_container)
    
    def set_channel(self, channel_info: ChannelInfo):
        """Update channel info display"""
        self.channel_info = channel_info
        self.name_label.setText(channel_info.display_name)
        self.followers_label.setText(f"{channel_info.followers_formatted} Followers")
        
        if channel_info.verified:
            self.verified_label.show()
        else:
            self.verified_label.hide()
        
        if channel_info.is_live:
            self.live_container.show()
        else:
            self.live_container.hide()
        
        # Load avatar
        if channel_info.avatar:
            thread = threading.Thread(target=self._fetch_avatar, args=(channel_info.avatar,), daemon=True)
            thread.start()
        
        self.show()
    
    def _fetch_avatar(self, url: str):
        """Fetch avatar in background"""
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(response.content))
                self.avatar_pixmap = pixmap.scaled(
                    88, 88,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                QTimer.singleShot(0, self._update_avatar)
        except Exception as e:
            logger.debug(f"Failed to load avatar: {e}")
    
    def _update_avatar(self):
        """Update avatar in main thread"""
        if self.avatar_pixmap:
            # Create circular mask
            size = 88
            rounded = QPixmap(size, size)
            rounded.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(rounded)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            path = QPainterPath()
            path.addEllipse(0, 0, size, size)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, self.avatar_pixmap)
            painter.end()
            
            self.avatar_label.setPixmap(rounded)


class DownloadPanel(QFrame):
    
    download_clicked = pyqtSignal()
    chat_download_clicked = pyqtSignal()
    cancel_clicked = pyqtSignal()
    browse_clicked = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_count = 0
        self.downloading = False
        self.chat_downloading = False
        self.current_video = ""
        self.progress = 0
        self.speed = ""
        self.eta = ""
        self.setup_ui()
    
    def setup_ui(self):
        self.setStyleSheet(f"""
            DownloadPanel {{
                background-color: rgba(16, 34, 16, 0.98);
                border-top: 2px solid {KickTheme.PRIMARY};
            }}
        """)
        
        # Start hidden - will slide in when VOD selected
        self.setMaximumHeight(0)
        self.setMinimumHeight(0)
        
        # Animation for slide effect
        self.slide_animation = QPropertyAnimation(self, b"maximumHeight")
        self.slide_animation.setDuration(300)
        self.slide_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 24)
        layout.setSpacing(16)
        
        # Progress section (hidden by default)
        self.progress_section = QWidget()
        progress_layout = QVBoxLayout(self.progress_section)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(8)
        
        # Status row
        status_layout = QHBoxLayout()
        
        self.status_container = QWidget()
        status_inner = QHBoxLayout(self.status_container)
        status_inner.setContentsMargins(0, 0, 0, 0)
        status_inner.setSpacing(8)
        
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {KickTheme.PRIMARY}; font-size: 8px;")
        status_inner.addWidget(self.status_dot)
        
        self.status_label = QLabel("DOWNLOADING: VIDEO NAME...")
        self.status_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.PRIMARY};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }}
        """)
        status_inner.addWidget(self.status_label)
        
        status_layout.addWidget(self.status_container)
        status_layout.addStretch()
        
        # Speed and ETA
        self.speed_label = QLabel("12.5 MB/s")
        self.speed_label.setStyleSheet(f"color: {KickTheme.TEXT_SECONDARY}; font-size: 11px; font-weight: bold;")
        status_layout.addWidget(self.speed_label)
        
        self.eta_label = QLabel("Est. 5m 20s left")
        self.eta_label.setStyleSheet(f"color: {KickTheme.TEXT_PRIMARY}; font-size: 11px; font-weight: bold;")
        status_layout.addWidget(self.eta_label)
        
        progress_layout.addLayout(status_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                border: none;
            }}
            QProgressBar::chunk {{
                background-color: {KickTheme.PRIMARY};
                border-radius: 4px;
            }}
        """)
        progress_layout.addWidget(self.progress_bar)
        
        self.progress_section.hide()
        layout.addWidget(self.progress_section)
        
        # Controls row
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(16)
        
        # Selected count badge
        self.selected_badge = QLabel("0 VOD SELECTED")
        self.selected_badge.setStyleSheet(f"""
            QLabel {{
                background-color: rgba(19, 236, 19, 0.2);
                color: {KickTheme.PRIMARY};
                border-radius: 12px;
                padding: 6px 12px;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }}
        """)
        controls_layout.addWidget(self.selected_badge)
        
        # Size estimate
        self.size_label = QLabel("0 GB approx.")
        self.size_label.setStyleSheet(f"color: {KickTheme.TEXT_SECONDARY}; font-size: 13px;")
        controls_layout.addWidget(self.size_label)
        
        controls_layout.addStretch()
        
        # Download path
        path_container = QWidget()
        path_layout = QHBoxLayout(path_container)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        
        self.path_input = QLineEdit()
        self.path_input.setReadOnly(True)
        self.path_input.setFixedWidth(300)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.path_input.setText(os.path.join(current_dir, "downloads"))
        self.path_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: rgba(255, 255, 255, 0.05);
                color: {KickTheme.TEXT_SECONDARY};
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                padding: 8px 12px 8px 32px;
                font-size: 12px;
            }}
        """)
        path_layout.addWidget(self.path_input)
        
        self.browse_btn = QPushButton("✏")
        self.browse_btn.setFixedSize(36, 36)
        self.browse_btn.clicked.connect(self.browse_clicked.emit)
        self.browse_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(255, 255, 255, 0.1);
                color: {KickTheme.TEXT_PRIMARY};
                border: none;
                border-radius: 8px;
                font-size: 14px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 0.2);
            }}
        """)
        path_layout.addWidget(self.browse_btn)
        
        controls_layout.addWidget(path_container)
        
        layout.addLayout(controls_layout)
        
        # Buttons row
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(12)
        
        # Chat download button
        self.chat_btn = QPushButton("💬 DOWNLOAD CHAT")
        self.chat_btn.setFixedHeight(56)
        self.chat_btn.clicked.connect(self._on_chat_button_click)
        self.chat_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(19, 236, 19, 0.15);
                color: {KickTheme.PRIMARY};
                border: 2px solid {KickTheme.PRIMARY};
                border-radius: 12px;
                font-size: 13px;
                font-weight: bold;
                letter-spacing: 1px;
                padding: 0 24px;
            }}
            QPushButton:hover {{
                background-color: rgba(19, 236, 19, 0.25);
            }}
            QPushButton:disabled {{
                background-color: rgba(255, 255, 255, 0.05);
                color: {KickTheme.TEXT_MUTED};
                border-color: rgba(255, 255, 255, 0.1);
            }}
        """)
        buttons_layout.addWidget(self.chat_btn, 1)
        
        # Video download button
        self.download_btn = QPushButton()
        self.download_btn.setFixedHeight(56)
        self.download_btn.clicked.connect(self._on_button_click)
        buttons_layout.addWidget(self.download_btn, 2)
        
        layout.addLayout(buttons_layout)
        
        self.update_button_state()
    
    def _on_chat_button_click(self):
        if self.chat_downloading:
            self.cancel_clicked.emit()
        else:
            self.chat_download_clicked.emit()
    
    def _on_button_click(self):
        if self.downloading:
            self.cancel_clicked.emit()
        else:
            self.download_clicked.emit()
    
    def update_button_state(self):
        """Update download button appearance"""
        enabled = self.selected_count > 0
        
        # Chat button state
        if self.chat_downloading:
            self.chat_btn.setText(f"💬 DOWNLOADING CHAT... {self.progress}%  ✕")
            self.chat_btn.setEnabled(True)
        else:
            self.chat_btn.setText("💬 DOWNLOAD CHAT")
            self.chat_btn.setEnabled(enabled and not self.downloading)
        
        # Video button state
        if self.downloading:
            self.download_btn.setText(f"⟳ DOWNLOADING...                                                    {self.progress}%  ✕")
            self.download_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(19, 236, 19, 0.2);
                    color: {KickTheme.PRIMARY};
                    border: 2px solid {KickTheme.PRIMARY};
                    border-radius: 12px;
                    font-size: 14px;
                    font-weight: bold;
                    letter-spacing: 2px;
                    text-align: left;
                    padding-left: 24px;
                }}
                QPushButton:hover {{
                    background-color: rgba(19, 236, 19, 0.3);
                }}
            """)
            self.chat_btn.setEnabled(False)
        else:
            if enabled:
                self.download_btn.setText(f"🎬 DOWNLOAD {self.selected_count} VOD{'S' if self.selected_count > 1 else ''}")
                self.download_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {KickTheme.PRIMARY};
                        color: #000000;
                        border: none;
                        border-radius: 12px;
                        font-size: 14px;
                        font-weight: bold;
                        letter-spacing: 2px;
                    }}
                    QPushButton:hover {{
                        background-color: {KickTheme.PRIMARY_LIGHT};
                    }}
                """)
            else:
                self.download_btn.setText("SELECT VIDEOS TO DOWNLOAD")
                self.download_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: rgba(255, 255, 255, 0.1);
                        color: {KickTheme.TEXT_MUTED};
                        border: 1px solid rgba(255, 255, 255, 0.1);
                        border-radius: 12px;
                        font-size: 14px;
                        font-weight: bold;
                        letter-spacing: 2px;
                    }}
                """)
            self.download_btn.setEnabled(enabled and not self.chat_downloading)
    
    def set_selected_count(self, count: int):
        """Update selected video count"""
        old_count = self.selected_count
        self.selected_count = count
        self.selected_badge.setText(f"{count} VOD{'S' if count != 1 else ''} SELECTED")
        
        # Estimate size (rough: ~2GB per hour, assume average 6 hours)
        estimated_gb = count * 2 * 6
        self.size_label.setText(f"~{estimated_gb} GB approx.")
        
        # Slide panel in/out based on selection
        if count > 0 and old_count == 0:
            # Slide in
            self.slide_animation.setStartValue(0)
            self.slide_animation.setEndValue(200)
            self.slide_animation.start()
        elif count == 0 and old_count > 0:
            # Slide out
            self.slide_animation.setStartValue(200)
            self.slide_animation.setEndValue(0)
            self.slide_animation.start()
        
        self.update_button_state()
    
    def set_downloading(self, downloading: bool, video_name: str = ""):
        """Set downloading state"""
        self.downloading = downloading
        self.current_video = video_name
        self.progress_section.setVisible(downloading or self.chat_downloading)
        
        if downloading:
            self.status_label.setText(f"DOWNLOADING VIDEO: {video_name[:30]}...")
        
        self.update_button_state()
    
    def set_chat_downloading(self, downloading: bool, video_name: str = ""):
        """Set chat downloading state"""
        self.chat_downloading = downloading
        self.current_video = video_name
        self.progress_section.setVisible(downloading or self.downloading)
        
        if downloading:
            self.status_label.setText(f"DOWNLOADING CHAT: {video_name[:30]}...")
        
        self.update_button_state()
    
    def set_progress(self, progress: int, speed: str = "", eta: str = ""):
        """Update download progress"""
        self.progress = progress
        self.speed = speed
        self.eta = eta
        
        self.progress_bar.setValue(progress)
        self.speed_label.setText(speed)
        self.eta_label.setText(f"Est. {eta} left" if eta else "")
        self.update_button_state()
    
    def set_download_path(self, path: str):
        """Set download path"""
        self.path_input.setText(path)


class KickDownloader(QMainWindow):
    
    def __init__(self):
        super().__init__()
        self.videos: List[VideoInfo] = []
        self.video_cards: List[VideoCard] = []
        self.channel_info: Optional[ChannelInfo] = None
        self.scraper_thread: Optional[ScraperThread] = None
        self.download_thread: Optional[DownloadThread] = None
        self.chat_download_thread: Optional[ChatDownloadThread] = None
        
        self.setup_ui()
    
    def setup_ui(self):
        self.setWindowTitle("Krayb's Kick Downloader")
        self.setGeometry(100, 100, 1200, 800)
        self.setMinimumSize(1000, 700)
        
        # Set dark theme
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {KickTheme.BG_DARK};
            }}
            QWidget {{
                background-color: transparent;
                color: {KickTheme.TEXT_PRIMARY};
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }}
            QScrollArea {{
                border: none;
                background-color: transparent;
            }}
            QScrollBar:vertical {{
                background-color: {KickTheme.BG_DARK};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background-color: rgba(255, 255, 255, 0.2);
                border-radius: 4px;
                min-height: 40px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: rgba(255, 255, 255, 0.3);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Content area
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        
        # Scroll area for main content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 20)  # Minimal bottom padding - panel slides in
        self.scroll_layout.setSpacing(0)
        
        # Search section
        self.create_search_section(self.scroll_layout)
        
        # Channel header
        self.channel_header = ChannelHeader()
        self.channel_header.select_all_btn.clicked.connect(self.toggle_select_all)
        self.scroll_layout.addWidget(self.channel_header)
        
        # Videos section header
        self.videos_header = QLabel("Available VODs")
        self.videos_header.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_PRIMARY};
                font-size: 18px;
                font-weight: bold;
                padding: 16px 16px 8px 16px;
                background: transparent;
            }}
        """)
        self.videos_header.hide()
        self.scroll_layout.addWidget(self.videos_header)
        
        # Video grid container
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(16, 8, 16, 16)
        self.grid_layout.setSpacing(16)
        self.scroll_layout.addWidget(self.grid_container)
        
        # Loading label
        self.loading_label = QLabel("Enter a Kick username to search for VODs")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setStyleSheet(f"""
            QLabel {{
                color: {KickTheme.TEXT_SECONDARY};
                font-size: 16px;
                padding: 60px;
                background: transparent;
            }}
        """)
        self.scroll_layout.addWidget(self.loading_label)
        
        self.scroll_layout.addStretch()
        
        scroll_area.setWidget(scroll_content)
        content_layout.addWidget(scroll_area)
        
        main_layout.addWidget(content_widget)
        
        # Bottom download panel
        self.download_panel = DownloadPanel()
        self.download_panel.download_clicked.connect(self.start_download)
        self.download_panel.chat_download_clicked.connect(self.start_chat_download)
        self.download_panel.cancel_clicked.connect(self.cancel_download)
        self.download_panel.browse_clicked.connect(self.browse_download_path)
        main_layout.addWidget(self.download_panel)
    
    def create_search_section(self, layout):
        """Create the search input section"""
        search_container = QWidget()
        search_container.setStyleSheet("background: transparent;")
        
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(16, 24, 16, 16)
        search_layout.setSpacing(0)
        
        # Search input container
        input_container = QFrame()
        input_container.setStyleSheet(f"""
            QFrame {{
                background-color: {KickTheme.BG_INPUT};
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
            }}
            QFrame:focus-within {{
                border: 1px solid {KickTheme.PRIMARY};
            }}
        """)
        
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(16, 0, 8, 0)
        input_layout.setSpacing(12)
        
        # Search icon
        search_icon = QLabel("🔍")
        search_icon.setStyleSheet(f"color: {KickTheme.TEXT_SECONDARY}; font-size: 16px;")
        input_layout.addWidget(search_icon)
        
        # Input field
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Search Kick username (e.g. xQc)...")
        self.username_input.setFixedHeight(56)
        self.username_input.returnPressed.connect(self.search_videos)
        self.username_input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {KickTheme.TEXT_PRIMARY};
                font-size: 15px;
                padding: 0;
            }}
            QLineEdit::placeholder {{
                color: rgba(157, 185, 157, 0.6);
            }}
        """)
        input_layout.addWidget(self.username_input)
        
        # Fetch button
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setFixedSize(80, 40)
        self.fetch_btn.clicked.connect(self.search_videos)
        self.fetch_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {KickTheme.PRIMARY};
                color: #000000;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {KickTheme.PRIMARY_LIGHT};
            }}
            QPushButton:pressed {{
                background-color: {KickTheme.PRIMARY_DARK};
            }}
        """)
        input_layout.addWidget(self.fetch_btn)
        
        search_layout.addWidget(input_container)
        
        layout.addWidget(search_container)
    
    def search_videos(self):
        """Start video search"""
        username = self.username_input.text().strip()
        if not username:
            QMessageBox.warning(self, "Error", "Please enter a username")
            return
        
        # Clear previous results
        self.clear_videos()
        
        # Show loading
        self.loading_label.setText("🔄 Searching for videos...")
        self.loading_label.show()
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("...")
        
        # Start scraper thread
        self.scraper_thread = ScraperThread(username)
        self.scraper_thread.videos_loaded.connect(self.on_videos_loaded)
        self.scraper_thread.error_occurred.connect(self.on_scraper_error)
        self.scraper_thread.progress_updated.connect(self.on_scraper_progress)
        self.scraper_thread.start()
    
    def on_videos_loaded(self, channel_info: ChannelInfo, videos: List[VideoInfo]):
        """Handle loaded videos"""
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")
        
        self.channel_info = channel_info
        self.videos = videos
        
        if not videos:
            self.loading_label.setText("No VODs found for this channel")
            return
        
        # Update channel header
        self.channel_header.set_channel(channel_info)
        
        # Show videos header
        self.videos_header.setText(f"Available VODs ({len(videos)})")
        self.videos_header.show()
        
        # Hide loading
        self.loading_label.hide()
        
        # Create video cards
        self.create_video_cards(videos)
    
    def on_scraper_error(self, error: str):
        """Handle scraper error"""
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")
        self.loading_label.setText(f"❌ Error: {error}")
        QMessageBox.critical(self, "Error", f"Failed to fetch videos:\n{error}")
    
    def on_scraper_progress(self, message: str):
        """Update loading message"""
        self.loading_label.setText(f"🔄 {message}")
    
    def clear_videos(self):
        """Clear video grid"""
        self.videos = []
        self.video_cards = []
        
        # Remove all cards from grid
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        
        self.channel_header.hide()
        self.videos_header.hide()
        self.download_panel.set_selected_count(0)
    
    def create_video_cards(self, videos: List[VideoInfo]):
        """Create video card widgets"""
        columns = 3  # 3 columns grid
        
        for i, video in enumerate(videos):
            card = VideoCard(video)
            card.clicked.connect(self.on_video_clicked)
            
            row = i // columns
            col = i % columns
            
            self.grid_layout.addWidget(card, row, col)
            self.video_cards.append(card)
    
    def on_video_clicked(self, video: VideoInfo):
        """Handle video selection"""
        selected_count = sum(1 for v in self.videos if v.selected)
        self.download_panel.set_selected_count(selected_count)
    
    def toggle_select_all(self):
        """Toggle select all videos"""
        # Check if all are selected
        all_selected = all(v.selected for v in self.videos)
        
        # Toggle all
        for card in self.video_cards:
            card.set_selected(not all_selected)
        
        selected_count = sum(1 for v in self.videos if v.selected)
        self.download_panel.set_selected_count(selected_count)
    
    def browse_download_path(self):
        """Open folder browser"""
        current_path = self.download_panel.path_input.text()
        path = QFileDialog.getExistingDirectory(
            self, "Select Download Folder", current_path
        )
        if path:
            self.download_panel.set_download_path(path)
    
    def start_download(self):
        """Start downloading selected videos"""
        selected_videos = [v for v in self.videos if v.selected]
        if not selected_videos:
            QMessageBox.warning(self, "Error", "Please select at least one video")
            return
        
        download_path = self.download_panel.path_input.text()
        
        # Mark cards as downloading
        for card in self.video_cards:
            if card.video.selected:
                card.set_downloading(True)
        
        # Update panel
        self.download_panel.set_downloading(True, selected_videos[0].title)
        
        # Start download thread
        self.download_thread = DownloadThread(selected_videos, download_path)
        self.download_thread.progress_updated.connect(self.on_download_progress)
        self.download_thread.download_completed.connect(self.on_video_downloaded)
        self.download_thread.download_error.connect(self.on_download_error)
        self.download_thread.all_completed.connect(self.on_all_downloads_completed)
        self.download_thread.start()
    
    def start_chat_download(self):
        """Start downloading chat for selected videos"""
        selected_videos = [v for v in self.videos if v.selected]
        if not selected_videos:
            QMessageBox.warning(self, "Error", "Please select at least one video")
            return
        
        download_path = self.download_panel.path_input.text()
        
        # Mark cards as downloading
        for card in self.video_cards:
            if card.video.selected:
                card.set_downloading(True)
        
        # Update panel
        self.download_panel.set_chat_downloading(True, selected_videos[0].title)
        
        # Start chat download thread
        self.chat_download_thread = ChatDownloadThread(selected_videos, download_path)
        self.chat_download_thread.progress_updated.connect(self.on_chat_download_progress)
        self.chat_download_thread.download_completed.connect(self.on_chat_downloaded)
        self.chat_download_thread.download_error.connect(self.on_chat_download_error)
        self.chat_download_thread.all_completed.connect(self.on_all_chat_downloads_completed)
        self.chat_download_thread.start()
    
    def on_chat_download_progress(self, video_id: str, progress: int, status: str):
        """Update chat download progress"""
        # Update card
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_progress(progress, status, "")
                break
        
        # Update panel
        self.download_panel.set_progress(progress, status, "")
    
    def on_chat_downloaded(self, video_id: str, filepath: str):
        """Handle single chat download completion"""
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_downloading(False)
                card.set_selected(False)
                break
        logger.info(f"Chat saved to: {filepath}")
    
    def on_chat_download_error(self, video_id: str, error: str):
        """Handle chat download error"""
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_downloading(False)
                break
        
        logger.error(f"Chat download error for {video_id}: {error}")
        QMessageBox.warning(self, "Error", f"Chat download failed: {error}")
    
    def on_all_chat_downloads_completed(self):
        """Handle all chat downloads completed"""
        self.download_panel.set_chat_downloading(False)
        self.download_panel.set_selected_count(0)
        QMessageBox.information(self, "Complete", "Chat downloads completed!")
    
    def on_download_progress(self, video_id: str, progress: int, speed: str, eta: str):
        """Update download progress"""
        # Update card
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_progress(progress, speed, eta)
                break
        
        # Update panel
        self.download_panel.set_progress(progress, speed, eta)
    
    def on_video_downloaded(self, video_id: str):
        """Handle single video download completion"""
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_downloading(False)
                card.set_selected(False)
                break
    
    def on_download_error(self, video_id: str, error: str):
        """Handle download error"""
        for card in self.video_cards:
            if card.video.video_id == video_id:
                card.set_downloading(False)
                break
        
        logger.error(f"Download error for {video_id}: {error}")
    
    def on_all_downloads_completed(self):
        """Handle all downloads completed"""
        self.download_panel.set_downloading(False)
        self.download_panel.set_selected_count(0)
        QMessageBox.information(self, "Complete", "All downloads completed!")
    
    def cancel_download(self):
        """Cancel ongoing downloads"""
        if self.download_thread:
            self.download_thread.cancel()
        
        if self.chat_download_thread:
            self.chat_download_thread.cancel()
        
        for card in self.video_cards:
            card.set_downloading(False)
        
        self.download_panel.set_downloading(False)
        self.download_panel.set_chat_downloading(False)


def main():
    app = QApplication(sys.argv)
    
    # Set application-wide font
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    
    window = KickDownloader()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
