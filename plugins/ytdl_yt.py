# Copyright @juktijol
# Channel t.me/juktijol
# YouTube downloader — yt-dlp powered
# ✅ FIXED: Cloudflare WARP proxy + bgutil POT + Node.js JS runtime
#           ytcookies.txt দিয়ে YouTube কাজ করে!

import os
import shutil
import asyncio
import tempfile
import subprocess
import socket
from time import time
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pyrogram.handlers import MessageHandler

from pyleaves import Leaves
from config import COMMAND_PREFIX
from utils.logging_setup import LOGGER
from utils.helper import (
    get_readable_file_size,
    get_readable_time,
    get_video_thumbnail,
    progressArgs,
)
from core import daily_limit, prem_plan1, prem_plan2, prem_plan3

# ─── yt-dlp import ───────────────────────────────────────────────────────────
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False
    LOGGER.error("yt-dlp not installed!")

# ─── pybalt import (fallback) ────────────────────────────────────────────────
try:
    from pybalt import download as pybalt_download_func
    PYBALT_AVAILABLE = True
except ImportError:
    PYBALT_AVAILABLE = False

# ─── Config ───────────────────────────────────────────────────────────────────
DOWNLOAD_DIR     = os.path.join(tempfile.gettempdir(), "ytdl_downloads")
MAX_FILE_SIZE    = 2 * 1024 * 1024 * 1024
FREE_FILE_SIZE   = 500 * 1024 * 1024
FREE_DAILY_LIMIT = 5
SESSION_EXPIRY   = 600
STALE_FILE_AGE   = 1800

WARP_PROXY     = "socks5://127.0.0.1:40000"
BGUTIL_POT_URL = os.environ.get("BGUTIL_POT_URL", "http://127.0.0.1:4416")
NODE_PATH      = os.environ.get("NODE_PATH", "/usr/bin/node")

# YouTube cookies file (ytcookies.txt এখানে থাকলে ব্যবহার হবে)
COOKIES_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ytcookies.txt")

# YouTube URL pattern check
import re as _re
_YT_PATTERN = _re.compile(
    r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/|youtube\.com/live/|"
    r"m\.youtube\.com/watch|music\.youtube\.com)"
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ytdl_sessions: dict = {}


# ─── WARP helpers ────────────────────────────────────────────────────────────

def _is_warp_available() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", 40000))
        s.close()
        return result == 0
    except Exception:
        return False


def _ensure_warp_connected():
    try:
        result = subprocess.run(
            ["warp-cli", "status"], capture_output=True, text=True, timeout=5
        )
        if "Connected" not in result.stdout:
            subprocess.run(["warp-cli", "connect"], timeout=10, capture_output=True)
            import time as _t
            _t.sleep(2)
    except Exception as e:
        LOGGER.warning(f"[WARP] connect failed: {e}")


# Startup
try:
    _ensure_warp_connected()
    if _is_warp_available():
        LOGGER.info("[WARP] Proxy available on port 40000 ✅")
    else:
        LOGGER.warning("[WARP] Proxy not available — YouTube may be blocked")
except Exception:
    pass


# ─── yt-dlp options ──────────────────────────────────────────────────────────

def _build_ydl_opts(use_proxy: bool = True) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtubepot-bgutilhttp": {
                "base_url": [BGUTIL_POT_URL],
            },
        },
        "buffersize": 1024 * 16,
        "concurrent_fragment_downloads": 1,
    }
    if os.path.exists(NODE_PATH):
        opts["js_runtimes"] = [f"node:{NODE_PATH}"]

    # ytcookies.txt থাকলে cookies হিসেবে ব্যবহার করো
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    if use_proxy and _is_warp_available():
        opts["proxy"] = WARP_PROXY

    return opts


# ─── Cleanup ─────────────────────────────────────────────────────────────────

def cleanup_stale_files():
    now = time()
    cleaned = 0
    try:
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    if now - os.path.getmtime(fpath) > STALE_FILE_AGE:
                        os.remove(fpath)
                        cleaned += 1
                except OSError:
                    pass
            for dname in dirs:
                dpath = os.path.join(root, dname)
                try:
                    if not os.listdir(dpath):
                        os.rmdir(dpath)
                except OSError:
                    pass
        if cleaned:
            LOGGER.info(f"[ytdl cleanup] {cleaned} stale file(s) removed")
    except Exception as e:
        LOGGER.warning(f"[ytdl cleanup] error: {e}")


def cleanup_expired_sessions():
    now = time()
    expired = [k for k, v in ytdl_sessions.items()
               if now - v.get("created_at", 0) > SESSION_EXPIRY]
    for k in expired:
        ytdl_sessions.pop(k, None)


cleanup_stale_files()


# ─── Error translator ────────────────────────────────────────────────────────

def _friendly_error(raw_error: str) -> str:
    err = raw_error.lower()
    if "sign in" in err or "not a bot" in err:
        return "🔒 YouTube bot detection। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
    if "age" in err and ("restrict" in err or "verif" in err):
        return "🔞 Age-restricted ভিডিও।"
    if "private" in err:
        return "🔒 Private ভিডিও।"
    if "copyright" in err or "blocked" in err:
        return "🚫 Copyright block।"
    if "not available" in err or "unavailable" in err:
        return "🚫 ভিডিওটি available নয়।"
    if "live" in err and "not supported" in err:
        return "📺 Live stream download হয় না।"
    if "connection refused" in err or "socks" in err:
        return "🌐 Proxy error। Bot restart করুন।"
    if "timeout" in err:
        return "🌐 Timeout। আবার চেষ্টা করুন।"
    clean = raw_error.replace("ERROR: ", "").strip()
    return f"⚠️ {clean[:200]}"


# ─── Premium check ────────────────────────────────────────────────────────────

async def is_premium_user(user_id: int) -> bool:
    current_time = datetime.utcnow()
    for col in [prem_plan1, prem_plan2, prem_plan3]:
        plan = await col.find_one({"user_id": user_id})
        if plan and plan.get("expiry_date", current_time) > current_time:
            return True
    return False


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def is_youtube_url(url: str) -> bool:
    """শুধুমাত্র YouTube URL গ্রহণ করবে।"""
    return bool(_YT_PATTERN.search(url))


# ─── Video info ──────────────────────────────────────────────────────────────

def get_video_info(url: str) -> tuple:
    url = normalize_url(url)
    last_error = ""

    for attempt, use_proxy in enumerate([True, False, True], 1):
        opts = {**_build_ydl_opts(use_proxy=use_proxy), "skip_download": True}
        if attempt == 3:
            opts["socket_timeout"] = 60
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    LOGGER.info(f"[ytdl] Info OK (attempt {attempt}) ✅")
                    return info, ""
        except Exception as e:
            last_error = str(e)
            LOGGER.warning(f"[ytdl] Info attempt {attempt} failed: {type(e).__name__}")

    return None, last_error


# ─── Download ────────────────────────────────────────────────────────────────

def download_media(url: str, output_path: str, format_id: str = None,
                   audio_only: bool = False, progress_data: dict = None) -> tuple:
    url    = normalize_url(url)
    outtmpl = os.path.join(output_path, "%(title).50s.%(ext)s")

    def _fmt():
        if audio_only:
            return "bestaudio/best"
        if format_id and format_id != "best":
            return f"{format_id}+bestaudio/best"
        return (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080][ext=mp4]/best[height<=1080]/best"
        )

    downloaded_file = []

    def progress_hook(d):
        if d["status"] == "finished":
            downloaded_file.append(d.get("filename", ""))
        elif d["status"] == "downloading" and progress_data is not None:
            progress_data["downloaded"] = d.get("downloaded_bytes", 0) or 0
            progress_data["total"]      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            progress_data["speed"]      = d.get("speed") or 0
            progress_data["eta"]        = d.get("eta") or 0

    def _find_file():
        if downloaded_file:
            fp = downloaded_file[-1]
            if audio_only and not fp.endswith(".mp3"):
                fp = os.path.splitext(fp)[0] + ".mp3"
            if os.path.exists(fp):
                return fp
        files = [os.path.join(output_path, f) for f in os.listdir(output_path)
                 if os.path.isfile(os.path.join(output_path, f))]
        return max(files, key=os.path.getmtime) if files else None

    postprocessors    = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}] if audio_only else []
    postprocessor_args = {} if audio_only else {"ffmpeg": ["-movflags", "+faststart"]}
    last_error = ""

    for attempt, use_proxy in enumerate([True, True, False], 1):
        opts = {
            **_build_ydl_opts(use_proxy=use_proxy and _is_warp_available()),
            "format":              "bestaudio/best" if (audio_only or attempt == 2) else _fmt(),
            "outtmpl":             outtmpl,
            "merge_output_format": "mp4" if not audio_only else None,
            "postprocessors":      postprocessors,
            "postprocessor_args":  postprocessor_args,
            "progress_hooks":      [progress_hook],
        }
        try:
            downloaded_file.clear()
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            fp = _find_file()
            if fp:
                LOGGER.info(f"[ytdl] Download OK (attempt {attempt}) → {fp}")
                return True, fp
        except Exception as e:
            last_error = str(e)
            LOGGER.warning(f"[ytdl] Download attempt {attempt} failed: {type(e).__name__}")

    return False, last_error


# ─── Quality keyboard ────────────────────────────────────────────────────────

def build_quality_keyboard(info: dict, chat_id: int) -> InlineKeyboardMarkup:
    formats = info.get("formats", [])
    seen, video_rows = set(), []
    for f in formats:
        height = f.get("height")
        fid    = f.get("format_id", "")
        vcodec = f.get("vcodec", "none")
        ext    = f.get("ext", "")
        if height and vcodec != "none" and height not in seen and ext in ("mp4", "webm", ""):
            seen.add(height)
            video_rows.append((height, fid))
    video_rows.sort(key=lambda x: x[0], reverse=True)

    buttons = []
    for height, fid in video_rows[:4]:
        label = f"🎬 {height}p HD" if height >= 720 else f"🎬 {height}p"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ytdl_v_{chat_id}_{fid}")])
    if not buttons:
        buttons.append([InlineKeyboardButton("🎬 Best Quality", callback_data=f"ytdl_v_{chat_id}_best")])
    buttons.append([InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"ytdl_a_{chat_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ytdl_cancel_{chat_id}")])
    return InlineKeyboardMarkup(buttons)


# ─── Progress updater ────────────────────────────────────────────────────────

def _make_progress_bar(pct: float, length: int = 20) -> str:
    filled = int(length * pct / 100)
    return "▓" * filled + "░" * (length - filled)


async def _ytdl_progress_updater(msg, progress_data: dict):
    last_text = ""
    while not progress_data.get("done"):
        await asyncio.sleep(3)
        if progress_data.get("done"):
            break
        dl    = progress_data.get("downloaded", 0)
        total = progress_data.get("total", 0)
        spd   = progress_data.get("speed", 0)
        eta   = progress_data.get("eta", 0)
        pct   = min((dl / total) * 100, 100) if total > 0 else 0
        text  = (
            f"📥 **Downloading**\n\n"
            f"`{_make_progress_bar(pct)}`\n"
            f"**Progress:** {pct:.2f}% | {get_readable_file_size(dl)}/{get_readable_file_size(total)}\n"
            f"**Speed:** {get_readable_file_size(spd)}/s  **ETA:** {get_readable_time(int(eta)) if eta else '...'}"
        )
        if text != last_text:
            try:
                await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                last_text = text
            except Exception:
                pass


# ─── pybalt fallback ─────────────────────────────────────────────────────────

async def pybalt_fallback_download(url: str, output_path: str, audio_only: bool = False) -> tuple:
    if not PYBALT_AVAILABLE:
        return False, "pybalt not installed"
    try:
        kwargs = {"url": url}
        if audio_only:
            kwargs.update({"downloadMode": "audio", "audioFormat": "mp3", "audioBitrate": "128"})
        result = None
        for folder_kwarg in ("folder_path", "path_folder"):
            try:
                result = await pybalt_download_func(**kwargs, **{folder_kwarg: output_path})
                break
            except TypeError as te:
                if folder_kwarg in str(te):
                    continue
                raise
        if result is None:
            result = await pybalt_download_func(**kwargs)
            if result and os.path.exists(str(result)):
                dest = os.path.join(output_path, os.path.basename(str(result)))
                shutil.move(str(result), dest)
                result = dest
        filepath = str(result) if result else None
        if filepath and os.path.exists(filepath):
            return True, filepath
        return False, "pybalt: file not found"
    except Exception as e:
        LOGGER.error(f"pybalt error: {e}")
        return False, str(e)


# ─── Handler ─────────────────────────────────────────────────────────────────

def setup_ytdl_handler(app: Client):

    async def ytdl_command(client: Client, message: Message):
        user_id = message.from_user.id

        if not YTDLP_AVAILABLE:
            await message.reply_text("❌ **yt-dlp ইনস্টল নেই!**", parse_mode=ParseMode.MARKDOWN)
            return

        if len(message.command) < 2:
            await message.reply_text(
                "🎬 **YouTube Downloader**\n\n"
                "**Usage:** `/yt <YouTube URL>`\n\n"
                "**Supported:** YouTube ভিডিও, Shorts, Live এবং YouTube Music!\n\n"
                "**Example:** `/yt https://youtu.be/xxxxx`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        text_parts = message.text.split(None, 1)
        url = text_parts[1].strip() if len(text_parts) > 1 else ""
        if not url:
            await message.reply_text("**Usage:** `/yt <YouTube URL>`", parse_mode=ParseMode.MARKDOWN)
            return

        # YouTube URL যাচাই
        if not is_youtube_url(normalize_url(url)):
            await message.reply_text(
                "❌ **শুধুমাত্র YouTube লিঙ্ক গ্রহণযোগ্য!**\n\n"
                "সঠিক URL দিন যেমন: `https://youtu.be/xxxxx`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        is_premium = await is_premium_user(user_id)
        if not is_premium:
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            rec = await daily_limit.find_one({"user_id": user_id})
            ytdl_count = 0
            if rec and rec.get("date") and rec["date"] >= today:
                ytdl_count = rec.get("ytdl_downloads", 0)
            if ytdl_count >= FREE_DAILY_LIMIT:
                await message.reply_text(
                    f"🚫 **Daily limit reached!** (Free: {FREE_DAILY_LIMIT}/day)\nUpgrade: /plans",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        warp_ok = _is_warp_available()
        status_msg = await message.reply_text(
            f"🔍 **Analyzing...**\n_{'🟢 WARP active' if warp_ok else '🟡 Direct connection'}_",
            parse_mode=ParseMode.MARKDOWN
        )

        loop = asyncio.get_event_loop()
        info, error_msg = await loop.run_in_executor(None, get_video_info, url)

        if not info:
            if PYBALT_AVAILABLE:
                cleanup_expired_sessions()
                ytdl_sessions[message.chat.id] = {
                    "user_id": user_id, "url": url, "info": {},
                    "message_id": message.id, "created_at": time(), "use_pybalt": True,
                }
                await status_msg.edit_text(
                    "📹 **Video Found (Cobalt Engine)**\n\n👇 **Quality বেছে নিন:**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎬 Best Quality", callback_data=f"ytdl_v_{message.chat.id}_best")],
                        [InlineKeyboardButton("🎵 Audio Only", callback_data=f"ytdl_a_{message.chat.id}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"ytdl_cancel_{message.chat.id}")],
                    ])
                )
                return
            await status_msg.edit_text(
                f"❌ **Download failed!**\n\n{_friendly_error(error_msg) if error_msg else 'Unknown error'}",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        title        = (info.get("title", "Unknown") or "Unknown")[:60]
        duration     = info.get("duration", 0) or 0
        uploader     = info.get("uploader", "Unknown") or "Unknown"
        duration_str = get_readable_time(int(duration)) if duration else "Unknown"

        cleanup_expired_sessions()
        ytdl_sessions[message.chat.id] = {
            "user_id": user_id, "url": url, "info": info,
            "message_id": message.id, "created_at": time(),
        }

        await status_msg.edit_text(
            f"📹 **{title}**\n\n"
            f"👤 **Channel:** {uploader}\n"
            f"⏱ **Duration:** {duration_str}\n\n"
            f"👇 **Quality বেছে নিন:**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_quality_keyboard(info, message.chat.id),
            disable_web_page_preview=True
        )

    @app.on_callback_query(filters.regex(r"^ytdl_(v|a|cancel)_"))
    async def ytdl_callback(client, callback_query):
        data    = callback_query.data
        chat_id = callback_query.message.chat.id
        user_id = callback_query.from_user.id

        session = ytdl_sessions.get(chat_id)
        if not session or session["user_id"] != user_id:
            await callback_query.answer("❌ Session expired!", show_alert=True)
            return

        if data.startswith("ytdl_cancel_"):
            await callback_query.message.edit_text("❌ **Cancelled.**", parse_mode=ParseMode.MARKDOWN)
            ytdl_sessions.pop(chat_id, None)
            await callback_query.answer()
            return

        url       = session["url"]
        is_audio  = data.startswith("ytdl_a_")
        format_id = None
        if data.startswith("ytdl_v_"):
            prefix    = f"ytdl_v_{chat_id}_"
            format_id = data[len(prefix):] if data.startswith(prefix) else None
            if format_id == "best":
                format_id = None

        await callback_query.answer("⏳ শুরু হচ্ছে...")

        is_premium = await is_premium_user(user_id)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if not is_premium:
            rec = await daily_limit.find_one({"user_id": user_id})
            ytdl_count = 0
            if rec and rec.get("date") and rec["date"] >= today:
                ytdl_count = rec.get("ytdl_downloads", 0)
            await daily_limit.update_one(
                {"user_id": user_id},
                {"$set": {"ytdl_downloads": ytdl_count + 1, "date": today},
                 "$inc": {"total_downloads": 1}},
                upsert=True
            )
        else:
            await daily_limit.update_one(
                {"user_id": user_id}, {"$inc": {"total_downloads": 1}}, upsert=True
            )

        warp_ok = _is_warp_available()
        await callback_query.message.edit_text(
            f"📥 **Downloading...**\n_{'🟢 WARP proxy' if warp_ok else '🟡 Direct'}_",
            parse_mode=ParseMode.MARKDOWN
        )

        cleanup_stale_files()
        user_dir = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        loop          = asyncio.get_event_loop()
        overall_start = time()
        use_pybalt    = session.get("use_pybalt", False)

        if use_pybalt:
            success, result = await pybalt_fallback_download(url, user_dir, is_audio)
        else:
            progress_data = {"downloaded": 0, "total": 0, "speed": 0, "eta": 0, "done": False}
            progress_task = asyncio.create_task(
                _ytdl_progress_updater(callback_query.message, progress_data)
            )
            try:
                success, result = await loop.run_in_executor(
                    None, download_media, url, user_dir, format_id, is_audio, progress_data
                )
            finally:
                progress_data["done"] = True
                try:
                    await progress_task
                except Exception:
                    pass

            if not success and PYBALT_AVAILABLE:
                await callback_query.message.edit_text(
                    "⚠️ **Cobalt engine দিয়ে চেষ্টা...**", parse_mode=ParseMode.MARKDOWN
                )
                success, result = await pybalt_fallback_download(url, user_dir, is_audio)

        if not success:
            await callback_query.message.edit_text(
                f"❌ **Download failed!**\n\n{_friendly_error(result)}",
                parse_mode=ParseMode.MARKDOWN
            )
            ytdl_sessions.pop(chat_id, None)
            return

        filepath  = result
        file_size = os.path.getsize(filepath)
        max_size  = MAX_FILE_SIZE if is_premium else FREE_FILE_SIZE

        if file_size > max_size:
            os.remove(filepath)
            await callback_query.message.edit_text(
                f"❌ **File অনেক বড়!**\n📦 `{get_readable_file_size(file_size)}` / Limit: `{get_readable_file_size(max_size)}`",
                parse_mode=ParseMode.MARKDOWN
            )
            ytdl_sessions.pop(chat_id, None)
            return

        await callback_query.message.edit_text(
            f"📤 **Uploading...**\n📦 `{get_readable_file_size(file_size)}`",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            info     = session.get("info", {})
            title    = (info.get("title") or "Downloaded Video")
            short_title = title[:50]
            uploader = info.get("uploader") or info.get("channel") or ""
            yt_url   = info.get("webpage_url") or session.get("url", "")
            caption  = (
                f"🎬 **{short_title}**\n"
                + (f"👤 {uploader}\n" if uploader else "")
                + (f"🔗 [YouTube Link]({yt_url})\n" if yt_url else "")
                + f"\n📥 Downloaded by @juktijol Bot"
            )
            duration = int(info.get("duration", 0) or 0)
            start_t  = time()

            if is_audio or filepath.endswith(".mp3"):
                await client.send_audio(
                    chat_id=chat_id, audio=filepath, caption=caption,
                    duration=duration, title=short_title, parse_mode=ParseMode.MARKDOWN,
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs("📤 Uploading", callback_query.message, start_t)
                )
            else:
                thumb_path = None
                try:
                    thumb_path = await get_video_thumbnail(filepath, duration)
                except Exception:
                    pass
                try:
                    await client.send_video(
                        chat_id=chat_id, video=filepath, caption=caption,
                        duration=duration, thumb=thumb_path,
                        parse_mode=ParseMode.MARKDOWN, supports_streaming=True,
                        progress=Leaves.progress_for_pyrogram,
                        progress_args=progressArgs("📤 Uploading", callback_query.message, start_t)
                    )
                finally:
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)

            elapsed = get_readable_time(int(time() - overall_start))
            await callback_query.message.edit_text(
                f"✅ **সফল!**\n⏱ `{elapsed}` | 📦 `{get_readable_file_size(file_size)}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            LOGGER.error(f"ytdl upload error: {e}")
            await callback_query.message.edit_text(
                f"❌ **Upload failed!**\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN
            )
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)
            try:
                if not os.listdir(user_dir):
                    os.rmdir(user_dir)
            except Exception:
                pass
            ytdl_sessions.pop(chat_id, None)

    app.add_handler(
        MessageHandler(
            ytdl_command,
            filters=filters.command("yt", prefixes=COMMAND_PREFIX)
                    & (filters.private | filters.group),
        ),
        group=1,
    )
