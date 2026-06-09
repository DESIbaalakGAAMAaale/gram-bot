# Copyright @juktijol
# Channel t.me/juktijol
# Facebook Video Downloader — yt-dlp powered
# ✅ Facebook cookies support from cookies.txt in the same folder

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

# ─── Config ───────────────────────────────────────────────────────────────────
DOWNLOAD_DIR     = os.path.join(tempfile.gettempdir(), "fbdl_downloads")
MAX_FILE_SIZE    = 2 * 1024 * 1024 * 1024
FREE_FILE_SIZE   = 500 * 1024 * 1024
FREE_DAILY_LIMIT = 5
SESSION_EXPIRY   = 600
STALE_FILE_AGE   = 1800

# ─── Cookies path: same folder as this script ────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(SCRIPT_DIR, "cookies.txt")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
fbdl_sessions: dict = {}


def check_cookies_validity() -> tuple:
    """
    cookies.txt ফাইলে required FB cookies আছে কিনা চেক করে।
    Returns: (has_required_cookies, missing_cookies_list)
    """
    REQUIRED = {"c_user", "xs", "datr", "fr", "sb"}
    if not os.path.isfile(COOKIES_FILE):
        return False, list(REQUIRED)
    try:
        found = set()
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                parts = line.split("	")
                if len(parts) >= 6:
                    cookie_name = parts[5].strip()
                    if cookie_name in REQUIRED:
                        found.add(cookie_name)
        missing = list(REQUIRED - found)
        return len(missing) == 0, missing
    except Exception:
        return False, list(REQUIRED)


# ─── Facebook URL validator ───────────────────────────────────────────────────

FACEBOOK_DOMAINS = (
    "facebook.com",
    "fb.com",
    "fb.watch",
    "m.facebook.com",
    "www.facebook.com",
)

def is_facebook_url(url: str) -> bool:
    url = url.lower().strip()
    return any(domain in url for domain in FACEBOOK_DOMAINS)


def extract_fb_video_url(url: str) -> str:
    """
    Group post URL বা যেকোনো FB URL থেকে direct video watch URL বানায়।
    যদি video ID পাওয়া না যায় তাহলে original URL ফেরত দেয়।
    """
    import re
    patterns = [
        r'[?&]v=(\d+)',           # ?v=123456
        r'/videos?/(\d+)',         # /video/123456 or /videos/123456
        r'/watch/\?v=(\d+)',       # /watch/?v=123456
        r'/reel/(\d+)',            # /reel/123456
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            LOGGER.info(f"[fbdl] Extracted video ID: {video_id} → using watch URL")
            return f"https://www.facebook.com/watch?v={video_id}"
    return url  # কোনো ID না পেলে original URL


def normalize_fb_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Convert mobile URL to desktop
    if "m.facebook.com/" in url:
        url = url.replace("m.facebook.com/", "www.facebook.com/", 1)
    # Remove idorvanity parameter (causes issues with yt-dlp)
    import re
    url = re.sub(r'\?idorvanity=\d+', '', url)
    url = re.sub(r'&idorvanity=\d+', '', url)
    # Try to convert group/post URL to direct video watch URL
    url = extract_fb_video_url(url)
    return url


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
            LOGGER.info(f"[fbdl cleanup] {cleaned} stale file(s) removed")
    except Exception as e:
        LOGGER.warning(f"[fbdl cleanup] error: {e}")


def cleanup_expired_sessions():
    now = time()
    expired = [k for k, v in fbdl_sessions.items()
               if now - v.get("created_at", 0) > SESSION_EXPIRY]
    for k in expired:
        fbdl_sessions.pop(k, None)


cleanup_stale_files()


# ─── yt-dlp options for Facebook ─────────────────────────────────────────────

def _build_fb_opts() -> dict:
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
        "buffersize": 1024 * 16,
        "concurrent_fragment_downloads": 1,
    }

    # ── Cookies: use cookies.txt from same folder if it exists ──
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        cookies_ok, missing = check_cookies_validity()
        if cookies_ok:
            LOGGER.info(f"[fbdl] Using cookies: {COOKIES_FILE} ✅ (all required cookies present)")
        else:
            LOGGER.warning(f"[fbdl] cookies.txt found but missing: {missing} — private videos may fail")
    else:
        LOGGER.warning(f"[fbdl] No cookies.txt found at {COOKIES_FILE} — private videos may fail")

    return opts


# ─── Error translator ────────────────────────────────────────────────────────

def _friendly_error(raw_error: str) -> str:
    err = raw_error.lower()
    if "sign in" in err or "login" in err or "not a bot" in err:
        return (
            "🔒 Facebook login required.\n\n"
            "**করণীয়:**\n"
            "• Facebook-এ login করে group page-এ যাও\n"
            "• সেখান থেকে fresh `cookies.txt` export করো\n"
            "• Required cookies: `c_user`, `xs`, `datr`, `fr`, `sb`"
        )
    if "no video formats" in err or "unsupported url" in err:
        return (
            "⚠️ Video format পাওয়া যায়নি বা URL সমর্থিত নয়।\n\n"
            "**করণীয়:**\n"
            "• Group post URL-এর বদলে direct video URL দাও:\n"
            "  `https://www.facebook.com/watch?v=VIDEO_ID`\n"
            "• `yt-dlp -U` দিয়ে yt-dlp আপডেট করো"
        )
    if "private" in err or "group" in err:
        return (
            "🔒 Private video/group — cookies দরকার।\n\n"
            "**করণীয়:**\n"
            "• Group page থেকে fresh cookies export করো\n"
            "• `/watch?v=VIDEO_ID` format-এর URL দিয়ে চেষ্টা করো"
        )
    if "copyright" in err or "blocked" in err:
        return "🚫 This video is blocked due to copyright."
    if "not available" in err or "unavailable" in err:
        return "🚫 This video is not available."
    if "live" in err and "not supported" in err:
        return "📺 Live streams cannot be downloaded."
    if "timeout" in err:
        return "🌐 Connection timed out. Please try again."
    if "404" in err or "not found" in err:
        return "❌ Video not found. Check the URL and try again."
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


# ─── Video info ──────────────────────────────────────────────────────────────

def get_video_info(url: str) -> tuple:
    url = normalize_fb_url(url)
    last_error = ""
    opts = {**_build_fb_opts(), "skip_download": True}
    for attempt in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    LOGGER.info(f"[fbdl] Info OK (attempt {attempt}) ✅")
                    return info, ""
        except Exception as e:
            last_error = str(e)
            LOGGER.warning(f"[fbdl] Info attempt {attempt} failed: {type(e).__name__}")
    return None, last_error


# ─── Download ────────────────────────────────────────────────────────────────

def download_media(url: str, output_path: str, format_id: str = None,
                   audio_only: bool = False, progress_data: dict = None) -> tuple:
    url = normalize_fb_url(url)
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

    postprocessors     = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}] if audio_only else []
    postprocessor_args = {} if audio_only else {"ffmpeg": ["-movflags", "+faststart"]}
    last_error = ""

    for attempt in range(1, 4):
        opts = {
            **_build_fb_opts(),
            "format":              "bestaudio/best" if audio_only else _fmt(),
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
                LOGGER.info(f"[fbdl] Download OK (attempt {attempt}) → {fp}")
                return True, fp
        except Exception as e:
            last_error = str(e)
            LOGGER.warning(f"[fbdl] Download attempt {attempt} failed: {type(e).__name__}")

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
        buttons.append([InlineKeyboardButton(label, callback_data=f"fbdl_v_{chat_id}_{fid}")])
    if not buttons:
        buttons.append([InlineKeyboardButton("🎬 Best Quality", callback_data=f"fbdl_v_{chat_id}_best")])
    buttons.append([InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"fbdl_a_{chat_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"fbdl_cancel_{chat_id}")])
    return InlineKeyboardMarkup(buttons)


# ─── Progress bar ─────────────────────────────────────────────────────────────

def _make_progress_bar(pct: float, length: int = 20) -> str:
    filled = int(length * pct / 100)
    return "▓" * filled + "░" * (length - filled)


async def _fbdl_progress_updater(msg, progress_data: dict):
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
            f"📥 **Downloading...**\n\n"
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


# ─── Handler ─────────────────────────────────────────────────────────────────

def setup_fbdl_handler(app: Client):

    async def fbdl_command(client: Client, message: Message):
        user_id = message.from_user.id

        if not YTDLP_AVAILABLE:
            await message.reply_text(
                "❌ **yt-dlp is not installed!**\n\nPlease install it: `pip install yt-dlp`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # ── No URL provided: ask user for a URL ──────────────────────────────
        if len(message.command) < 2:
            cookies_status = (
                "✅ **Cookies loaded** — private/login-required videos supported."
                if os.path.isfile(COOKIES_FILE)
                else "⚠️ **No cookies.txt found** — only public videos will work."
            )
            await message.reply_text(
                "📘 **Facebook Video Downloader**\n\n"
                f"{cookies_status}\n\n"
                "**Usage:** `/fbdl <Facebook URL>`\n\n"
                "**Supported links:**\n"
                "• `facebook.com/...` posts & reels\n"
                "• `fb.watch/...` short links\n"
                "• `m.facebook.com/...` mobile links\n\n"
                "**Example:**\n"
                "`/fbdl https://www.facebook.com/watch?v=123456789`\n\n"
                "📌 _Send me a Facebook video link to get started!_",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        text_parts = message.text.split(None, 1)
        url = text_parts[1].strip() if len(text_parts) > 1 else ""

        if not url:
            await message.reply_text(
                "❓ **No URL provided.**\n\n**Usage:** `/fbdl <Facebook URL>`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # ── Check if it's a Facebook URL ─────────────────────────────────────
        if not is_facebook_url(url):
            await message.reply_text(
                "❌ **Invalid URL!**\n\n"
                "This bot only supports **Facebook** videos.\n\n"
                "Please send a valid Facebook link:\n"
                "• `https://www.facebook.com/watch?v=...`\n"
                "• `https://fb.watch/...`\n"
                "• `https://www.facebook.com/reel/...`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # ── Daily limit check ─────────────────────────────────────────────────
        is_premium = await is_premium_user(user_id)
        if not is_premium:
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            rec = await daily_limit.find_one({"user_id": user_id})
            fbdl_count = 0
            if rec and rec.get("date") and rec["date"] >= today:
                fbdl_count = rec.get("fbdl_downloads", 0)
            if fbdl_count >= FREE_DAILY_LIMIT:
                await message.reply_text(
                    f"🚫 **Daily limit reached!**\n\n"
                    f"Free users can download **{FREE_DAILY_LIMIT} videos/day**.\n"
                    f"Upgrade to premium for unlimited downloads: /plans",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        # ── Cookies status info ───────────────────────────────────────────────
        cookies_ok = os.path.isfile(COOKIES_FILE)
        _ck_valid, _ck_missing = check_cookies_validity()
        if cookies_ok and _ck_valid:
            _ck_status = "🍪 Cookies active ✅"
        elif cookies_ok and not _ck_valid:
            _ck_status = f"⚠️ Cookies incomplete (missing: {', '.join(_ck_missing)})"
        else:
            _ck_status = "⚠️ No cookies — public only"
        status_msg = await message.reply_text(
            f"🔍 **Fetching video info...**\n"
            f"_{_ck_status}_",
            parse_mode=ParseMode.MARKDOWN
        )

        loop = asyncio.get_event_loop()
        info, error_msg = await loop.run_in_executor(None, get_video_info, url)

        if not info:
            err_text = _friendly_error(error_msg) if error_msg else "Unknown error."
            hint = (
                "\n\n💡 **Tip:** Add a `cookies.txt` file next to this bot's script to access private videos."
                if not cookies_ok else ""
            )
            await status_msg.edit_text(
                f"❌ **Could not fetch video!**\n\n{err_text}{hint}",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        title        = (info.get("title", "Unknown") or "Unknown")[:60]
        duration     = info.get("duration", 0) or 0
        uploader     = info.get("uploader", "Unknown") or "Unknown"
        duration_str = get_readable_time(int(duration)) if duration else "Unknown"

        cleanup_expired_sessions()
        fbdl_sessions[message.chat.id] = {
            "user_id": user_id, "url": url, "info": info,
            "message_id": message.id, "created_at": time(),
        }

        await status_msg.edit_text(
            f"📘 **{title}**\n\n"
            f"👤 **Page/User:** {uploader}\n"
            f"⏱ **Duration:** {duration_str}\n"
            f"{'🍪 _Cookies: Active_' if cookies_ok else '⚠️ _Cookies: Not found_'}\n\n"
            f"👇 **Choose download quality:**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_quality_keyboard(info, message.chat.id),
            disable_web_page_preview=True
        )

    @app.on_callback_query(filters.regex(r"^fbdl_(v|a|cancel)_"))
    async def fbdl_callback(client, callback_query):
        data    = callback_query.data
        chat_id = callback_query.message.chat.id
        user_id = callback_query.from_user.id

        session = fbdl_sessions.get(chat_id)
        if not session or session["user_id"] != user_id:
            await callback_query.answer("❌ Session expired! Send the link again.", show_alert=True)
            return

        if data.startswith("fbdl_cancel_"):
            await callback_query.message.edit_text(
                "❌ **Download cancelled.**",
                parse_mode=ParseMode.MARKDOWN
            )
            fbdl_sessions.pop(chat_id, None)
            await callback_query.answer()
            return

        url      = session["url"]
        is_audio = data.startswith("fbdl_a_")
        format_id = None
        if data.startswith("fbdl_v_"):
            prefix    = f"fbdl_v_{chat_id}_"
            format_id = data[len(prefix):] if data.startswith(prefix) else None
            if format_id == "best":
                format_id = None

        await callback_query.answer("⏳ Starting download...")

        is_premium = await is_premium_user(user_id)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if not is_premium:
            rec = await daily_limit.find_one({"user_id": user_id})
            fbdl_count = 0
            if rec and rec.get("date") and rec["date"] >= today:
                fbdl_count = rec.get("fbdl_downloads", 0)
            await daily_limit.update_one(
                {"user_id": user_id},
                {"$set": {"fbdl_downloads": fbdl_count + 1, "date": today},
                 "$inc": {"total_downloads": 1}},
                upsert=True
            )
        else:
            await daily_limit.update_one(
                {"user_id": user_id}, {"$inc": {"total_downloads": 1}}, upsert=True
            )

        cookies_ok = os.path.isfile(COOKIES_FILE)
        await callback_query.message.edit_text(
            f"📥 **Downloading Facebook video...**\n"
            f"_{'🍪 Cookies: Active' if cookies_ok else '⚠️ Cookies: Not found'}_",
            parse_mode=ParseMode.MARKDOWN
        )

        cleanup_stale_files()
        user_dir = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        loop          = asyncio.get_event_loop()
        overall_start = time()

        progress_data = {"downloaded": 0, "total": 0, "speed": 0, "eta": 0, "done": False}
        progress_task = asyncio.create_task(
            _fbdl_progress_updater(callback_query.message, progress_data)
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

        if not success:
            hint = (
                "\n\n💡 **Tip:** Make sure your `cookies.txt` is valid and not expired."
                if cookies_ok else
                "\n\n💡 **Tip:** Add a valid `cookies.txt` file to download private videos."
            )
            await callback_query.message.edit_text(
                f"❌ **Download failed!**\n\n{_friendly_error(result)}{hint}",
                parse_mode=ParseMode.MARKDOWN
            )
            fbdl_sessions.pop(chat_id, None)
            return

        filepath  = result
        file_size = os.path.getsize(filepath)
        max_size  = MAX_FILE_SIZE if is_premium else FREE_FILE_SIZE

        if file_size > max_size:
            os.remove(filepath)
            await callback_query.message.edit_text(
                f"❌ **File too large!**\n\n"
                f"📦 Size: `{get_readable_file_size(file_size)}`\n"
                f"📏 Limit: `{get_readable_file_size(max_size)}`\n\n"
                f"{'Try a lower quality.' if not is_premium else ''}",
                parse_mode=ParseMode.MARKDOWN
            )
            fbdl_sessions.pop(chat_id, None)
            return

        await callback_query.message.edit_text(
            f"📤 **Uploading to Telegram...**\n📦 `{get_readable_file_size(file_size)}`",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            info     = session.get("info", {})
            title    = ((info.get("title") or "Facebook Video"))[:50]
            caption  = f"**{title}**\n\n📥 Downloaded by @juktijol Bot"
            duration = int(info.get("duration", 0) or 0)
            start_t  = time()

            if is_audio or filepath.endswith(".mp3"):
                await client.send_audio(
                    chat_id=chat_id, audio=filepath, caption=caption,
                    duration=duration, title=title, parse_mode=ParseMode.MARKDOWN,
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
                f"✅ **Done!**\n\n"
                f"⏱ Time: `{elapsed}` | 📦 Size: `{get_readable_file_size(file_size)}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            LOGGER.error(f"fbdl upload error: {e}")
            await callback_query.message.edit_text(
                f"❌ **Upload failed!**\n`{str(e)[:200]}`",
                parse_mode=ParseMode.MARKDOWN
            )
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)
            try:
                if not os.listdir(user_dir):
                    os.rmdir(user_dir)
            except Exception:
                pass
            fbdl_sessions.pop(chat_id, None)

    app.add_handler(
        MessageHandler(
            fbdl_command,
            filters=filters.command("fbdl", prefixes=COMMAND_PREFIX)
                    & (filters.private | filters.group),
        ),
        group=1,
    )
