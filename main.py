#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║      Telegram Voice Chat Music Bot  🎵           ║
║      YouTube • Queue • Loop • Volume             ║
╚══════════════════════════════════════════════════╝
Cài thư viện:
    py -3.11 -m pip install py-tgcalls pyrofork yt-dlp requests aiofiles

Chạy:
    py -3.11 vc_bot_fixed.py
"""

# ── Fix event loop cho mọi Python version ────────
import asyncio
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# ── Cài ffmpeg nếu chưa có ───────────────────────
import subprocess as _sp, shutil as _sh
if not _sh.which("ffmpeg"):
    print("[setup] Cài ffmpeg...")
    _sp.run("apt-get install -y ffmpeg 2>/dev/null || "
            "apk add ffmpeg 2>/dev/null || "
            "yum install -y ffmpeg 2>/dev/null || true", shell=True)

import os, re, sys, logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import yt_dlp

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream
try:
    from pytgcalls.types import AudioQuality, VideoQuality
    _HQ_AUDIO  = AudioQuality.HIGH
    _HQ_VIDEO  = VideoQuality.HD_720p
except Exception:
    try:
        from pytgcalls.types import AudioQuality
        _HQ_AUDIO = AudioQuality.HIGH
    except Exception:
        _HQ_AUDIO = None
    _HQ_VIDEO = None

# Stream end events — tên khác nhau tuỳ version
try:
    from pytgcalls.types.stream import StreamAudioEnded, StreamVideoEnded
    _HAS_STREAM_EVENTS = True
except ImportError:
    _HAS_STREAM_EVENTS = False

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pytgcalls").setLevel(logging.WARNING)
log = logging.getLogger("vcbot")

# ══════════════════════════════════════════════════
#  CẤU HÌNH
# ══════════════════════════════════════════════════
API_ID    = 39030508
API_HASH  = "c7feab6d38db177b863ad909e4f66f0b"
BOT_TOKEN = "8254987879:AAGCLxCes79aDF6rGsZl1L4oPZUfTqj7uw0"

# ══════════════════════════════════════════════════

# ══════════════════════════════════════════════════
#  Data classes
# ══════════════════════════════════════════════════
@dataclass
class Track:
    title:        str
    url:          str
    duration:     int
    thumbnail:    str  = ""
    requester:    str  = ""
    requester_id: int  = 0     # user_id của người yêu cầu
    source:       str  = "yt"
    is_video:     bool = False

@dataclass
class GState:
    queue:    deque           = field(default_factory=deque)
    current:  Optional[Track] = None
    loop:     bool            = False
    volume:   int             = 100
    paused:   bool            = False
    np_msg:   int             = 0
    tmp_file: str             = ""   # file tạm đang stream — xoá sau khi xong

_states: dict[int, GState] = {}

def st(cid: int) -> GState:
    if cid not in _states:
        _states[cid] = GState()
    return _states[cid]

# ══════════════════════════════════════════════════
#  Search & Stream helpers
# ══════════════════════════════════════════════════
def _is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://") or "youtu" in text

_COOKIES_FILE = "cookies.txt" if os.path.exists("cookies.txt") else None
if _COOKIES_FILE:
    import logging as _l; _l.getLogger("vcbot").info("Cookies file found: %s", _COOKIES_FILE)
else:
    import logging as _l; _l.getLogger("vcbot").warning("No cookies.txt found — YouTube may block requests")

# PO Token để bypass YouTube bot check (không cần cookies)
_PO_TOKEN = ""  # Để trống — dùng cookies thay thế

import shutil as _shutil

def _find_ffmpeg():
    for cmd in ["ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/nix/store/*/bin/ffmpeg"]:
        path = _shutil.which(cmd) or (cmd if cmd.startswith("/") and __import__("os").path.exists(cmd) else None)
        if path:
            return __import__("os").path.dirname(path)
    return None

_FFMPEG_LOC = _find_ffmpeg()

def _yt_opts(extra: dict = {}) -> dict:
    opts = {
        "quiet":        True,
        "no_warnings":  True,
        "check_formats": False,
        "extractor_args": {
            "youtube": {
                # Thử nhiều client — nếu 1 bị block thì dùng cái khác
                "player_client": ["tv_embedded", "android_music", "android", "web_creator", "web"],
                "player_skip":   ["webpage"],
            }
        },
        **extra
    }
    if _COOKIES_FILE:
        opts["cookiefile"] = _COOKIES_FILE
    # Dùng OAuth token nếu có
    if os.path.exists("oauth_token.json"):
        opts["username"] = "oauth2"
        opts["password"] = ""
    if _FFMPEG_LOC:
        opts["ffmpeg_location"] = _FFMPEG_LOC
    return opts

def _search_yt(query: str, n: int = 5) -> list[Track]:
    opts = _yt_opts({"extract_flat": True})
    # Nếu là link thì lấy thông tin trực tiếp
    if _is_url(query):
        with yt_dlp.YoutubeDL(_yt_opts()) as ydl:
            info = ydl.extract_info(query, download=False)
            if info:
                return [Track(
                    title     = info.get("title", "?"),
                    url       = query,
                    duration  = int(info.get("duration") or 0),
                    thumbnail = info.get("thumbnail", ""),
                    source    = "yt",
                )]
        return []
    # Tìm kiếm bình thường
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)

    return [
        Track(
            title     = e.get("title", "?"),
            url       = f"https://youtube.com/watch?v={e['id']}",
            duration  = int(e.get("duration") or 0),
            thumbnail = e.get("thumbnail", ""),
            source    = "yt",
        )
        for e in (res.get("entries") or [])[:n] if e
    ]

import tempfile, glob

def _get_stream_url(track: Track) -> str:
    """Tải file audio xuống rồi trả về đường dẫn file — đảm bảo phát được."""
    tmpdir = tempfile.mkdtemp()
    opts = _yt_opts({
        "format":               "bestaudio/best",
        "outtmpl":              os.path.join(tmpdir, "audio.%(ext)s"),
        "check_formats":        False,
        "no_check_certificate": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        },
        # Extract audio bằng ffmpeg
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        }],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([track.url])
    
    files = glob.glob(os.path.join(tmpdir, "*.mp3"))
    if not files:
        files = glob.glob(os.path.join(tmpdir, "*"))
    if not files:
        raise Exception("Không tải được file audio")
    
    log.info("Downloaded: %s (%.1f MB)", os.path.basename(files[0]), os.path.getsize(files[0])/1024/1024)
    return files[0]

def _get_video_urls(track: Track):
    """Trả về URL video chất lượng thấp để giảm lag."""
    opts = {
        # 360p để giảm băng thông — đủ để xem trong VC
        "format":      "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best",
        "quiet":       True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(track.url, download=False)
        formats = info.get("formats", [])
        audio_url = None
        video_url = None
        for f in reversed(formats):
            if f.get("acodec") != "none" and f.get("vcodec") == "none" and not audio_url:
                audio_url = f["url"]
            if f.get("vcodec") != "none" and f.get("acodec") == "none" and not video_url:
                video_url = f["url"]
            if audio_url and video_url:
                break
        if not audio_url or not video_url:
            url = info.get("url") or formats[-1]["url"]
            return url, url
        return audio_url, video_url

def _fmt(s: int) -> str:
    if s <= 0: return "?"
    m, sec = divmod(s, 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

# ══════════════════════════════════════════════════
#  2 clients:
#  - userbot: dùng API_ID/HASH của bạn → join VC
#  - bot: dùng BOT_TOKEN → nhận lệnh từ user
# ══════════════════════════════════════════════════
userbot = Client(
    "vcbot_userbot",        # lưu session vào file vcbot_userbot.session
    api_id=API_ID,
    api_hash=API_HASH,
    sleep_threshold=60,
)
bot = Client(
    "vcbot_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=60,
)
# app = bot (dùng cho handlers)
app   = bot
calls = PyTgCalls(userbot)  # PyTgCalls dùng userbot để join VC

# ══════════════════════════════════════════════════
#  UI helpers
# ══════════════════════════════════════════════════
def _np_kb(g: GState) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Resume" if g.paused else "⏸ Pause", callback_data="vc_pause"),
            InlineKeyboardButton("⏭ Skip",   callback_data="vc_skip"),
            InlineKeyboardButton("⏹ Stop",   callback_data="vc_stop"),
        ],
        [
            InlineKeyboardButton("🔉",               callback_data="vc_vdown"),
            InlineKeyboardButton(f"🔊 {g.volume}%",  callback_data="vc_vol"),
            InlineKeyboardButton("🔊",               callback_data="vc_vup"),
        ],
        [
            InlineKeyboardButton("🔁 Loop ON" if g.loop else "➡️ Loop OFF", callback_data="vc_loop"),
            InlineKeyboardButton("📋 Queue",  callback_data="vc_queue"),
        ],
    ])

async def _send_np(client: Client, cid: int, track: Track):
    g = st(cid)
    text = (
        f"🎵 **{track.title}**\n"
        f"⏱ `{_fmt(track.duration)}`\n"
        f"👤 {track.requester}\n"
        f"📦 YouTube\n"
        f"📋 Hàng chờ: {len(g.queue)} bài"
    )
    if g.np_msg:
        try:
            await client.delete_messages(cid, g.np_msg)
        except Exception:
            pass
    try:
        msg = (
            await client.send_photo(cid, track.thumbnail, caption=text, reply_markup=_np_kb(g))
            if track.thumbnail
            else await client.send_message(cid, text, reply_markup=_np_kb(g))
        )
        g.np_msg = msg.id
    except Exception as e:
        log.error("send_np error: %s", e)

async def _update_np(client: Client, cid: int):
    g = st(cid)
    if not g.np_msg:
        return
    try:
        await client.edit_message_reply_markup(cid, g.np_msg, reply_markup=_np_kb(g))
    except Exception:
        pass

# ══════════════════════════════════════════════════
#  Play engine
# ══════════════════════════════════════════════════
async def _play_next(client: Client, cid: int):
    g = st(cid)

    if g.loop and g.current:
        track = g.current
    elif g.queue:
        track = g.queue.popleft()
    else:
        # Hết bài → thoát VC
        g.current = None
        if g.np_msg:
            try:
                await client.delete_messages(cid, g.np_msg)
            except Exception:
                pass
            g.np_msg = 0
        try:
            await calls.leave_call(cid)
            log.info("Userbot left VC in %d (queue empty)", cid)
        except Exception as e:
            log.warning("Auto-leave error: %s", e)
        await client.send_message(cid, "✅ Hết nhạc. Userbot đã thoát VC.")
        return

    # Xoá file tạm của bài trước
    if g.tmp_file and os.path.isfile(g.tmp_file):
        try:
            tmpdir = os.path.dirname(g.tmp_file)
            os.remove(g.tmp_file)
            os.rmdir(tmpdir)
        except Exception:
            pass
        g.tmp_file = ""

    g.current = track
    g.paused  = False

    try:
        if track.is_video:
            audio_url, video_url = await asyncio.get_event_loop().run_in_executor(
                None, _get_video_urls, track
            )
            try:
                from pytgcalls.types import VideoQuality
                ms = MediaStream(
                    video_url,
                    audio_path=audio_url,
                    video_parameters=VideoQuality.HD_720p,
                )
            except Exception:
                try:
                    ms = MediaStream(video_url, audio_path=audio_url)
                except Exception:
                    ms = MediaStream(video_url)
        else:
            # Tải file trong khi giữ kết nối bằng keepalive
            async def _keepalive():
                while True:
                    try:
                        await userbot.invoke(
                            __import__("pyrogram.raw.functions.updates", fromlist=["GetState"]).GetState()
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(20)

            ka_task = asyncio.create_task(_keepalive())
            try:
                stream_url = await asyncio.get_event_loop().run_in_executor(
                    None, _get_stream_url, track
                )
            finally:
                ka_task.cancel()

            try:
                from pytgcalls.types import AudioQuality
                ms = MediaStream(stream_url, audio_parameters=AudioQuality.HIGH)
            except Exception:
                ms = MediaStream(stream_url)
            if os.path.isfile(stream_url):
                g.tmp_file = stream_url

        await calls.play(cid, ms)
        await _send_np(client, cid, track)
        log.info("▶ Playing%s: %s [chat=%d]", " [VIDEO]" if track.is_video else "", track.title, cid)
    except Exception as e:
        log.error("_play_next error: %s", e)
        err_low = str(e).lower()
        if "no active" in err_low or "groupcall" in err_low or "not found" in err_low or "bot_method" in err_low or "invalid" in err_low:
            await client.send_message(
                cid,
                "❌ Chua co Voice Chat! Vao group → Voice Chat → Start Voice Chat → roi /play lai"
            )
        else:
            await client.send_message(cid, f"❌ Lỗi phát nhạc: `{e}`\nBỏ qua, thử bài tiếp…")
            if g.queue:
                await _play_next(client, cid)

# ══════════════════════════════════════════════════
#  Stream end event handler
#  Tương thích cả version cũ lẫn mới của py-tgcalls
# ══════════════════════════════════════════════════
@calls.on_update()
async def _on_update(_, update):
    try:
        cid = update.chat_id
    except AttributeError:
        return

    cls = type(update).__name__

    # Bỏ qua participant updates — không phải stream end
    if "Participant" in cls or "Member" in cls:
        return

    log.info("VC update: %s in %d", cls, cid)

    should_next = False
    if _HAS_STREAM_EVENTS:
        if isinstance(update, (StreamAudioEnded, StreamVideoEnded)):
            should_next = True
    # Kiểm tra theo tên class cho mọi version
    if "End" in cls or "Ended" in cls or "Finish" in cls or "Complete" in cls or "Stopped" in cls:
        should_next = True

    if should_next:
        log.info("Stream ended in %d → next track", cid)
        await _play_next(bot, cid)

# ══════════════════════════════════════════════════
#  Search result cache + keyboard
# ══════════════════════════════════════════════════
_cache: dict[int, list[Track]] = {}

def _search_kb(tracks: list[Track], src: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"▶️ {t.title[:38]} [{_fmt(t.duration)}]",
            callback_data=f"pick|{src}|{i}",
        )]
        for i, t in enumerate(tracks)
    ]
    rows.append([InlineKeyboardButton("❌ Huỷ", callback_data="pick_cancel")])
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════
@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    await msg.reply(
        "🎵 Voice Chat Music Bot\n\n"
        "`/play <tên bài>` — Tìm và phát từ YouTube\n"
        "`/skip`           — Bỏ qua bài hiện tại\n"
        "`/stop`           — Dừng và thoát VC\n"
        "`/pause`          — Tạm dừng\n"
        "`/resume`         — Tiếp tục phát\n"
        "`/queue`          — Xem hàng chờ\n"
        "`/np`             — Bài đang phát\n"
        "`/volume 80`      — Chỉnh âm lượng (0–200)\n"
        "`/loop`           — Bật / tắt lặp lại\n"
        "`/clear`          — Xoá hàng chờ\n\n"
        "⚠️ Bot phải là **admin** trong group và group phải có **Voice Chat đang mở**."
    )

@app.on_message(filters.command("play") & filters.group)
async def cmd_play(client: Client, msg: Message):
    q = " ".join(msg.command[1:]).strip()
    if not q:
        await msg.reply("❓ Dùng: /play <tên bài hoặc link YouTube>")
        return
    s = await msg.reply(f"🔍 Đang tìm **{q}** trên YouTube…")
    requester    = msg.from_user.first_name if msg.from_user else "?"
    requester_id = msg.from_user.id if msg.from_user else 0
    try:
        tracks = await asyncio.get_event_loop().run_in_executor(None, _search_yt, q, 5)
    except Exception as e:
        await s.edit(f"❌ Lỗi tìm kiếm: {e}")
        return
    if not tracks:
        await s.edit("😔 Không tìm thấy kết quả.")
        return
    for t in tracks:
        t.requester    = requester
        t.requester_id = requester_id
    await s.delete()
    m = await msg.reply(f"🎵 **{q}** — chọn bài:", reply_markup=_search_kb(tracks, "yt"))
    _cache[m.id] = tracks

@app.on_message(filters.command("video") & filters.group)
async def cmd_video(client: Client, msg: Message):
    q = " ".join(msg.command[1:]).strip()
    if not q:
        await msg.reply("❓ Dùng: /video <tên video hoặc link YouTube>")
        return
    s = await msg.reply(f"🔍 Đang tìm video **{q}**…")
    requester    = msg.from_user.first_name if msg.from_user else "?"
    requester_id = msg.from_user.id if msg.from_user else 0
    try:
        tracks = await asyncio.get_event_loop().run_in_executor(None, _search_yt, q, 5)
    except Exception as e:
        await s.edit(f"❌ Lỗi tìm kiếm: {e}")
        return
    if not tracks:
        await s.edit("😔 Không tìm thấy kết quả.")
        return
    for t in tracks:
        t.requester    = requester
        t.requester_id = requester_id
        t.is_video     = True
    await s.delete()
    m = await msg.reply(f"🎬 **{q}** — chọn video:", reply_markup=_search_kb(tracks, "yt"))
    _cache[m.id] = tracks

@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(client: Client, msg: Message):
    g = st(msg.chat.id)
    if not g.current:
        await msg.reply("❌ Không có bài nào đang phát.")
        return

    user_id = msg.from_user.id if msg.from_user else 0

    # Chỉ người yêu cầu bài mới được skip
    if g.current.requester_id != user_id:
        await msg.reply(f"❌ Chỉ **{g.current.requester}** (người yêu cầu) mới được skip bài này!")
        return

    title  = g.current.title
    g.loop = False
    g.current = None
    await msg.reply(f"⏭ Skip: {title}")
    await _play_next(client, msg.chat.id)

@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(client: Client, msg: Message):
    g = st(msg.chat.id)
    g.queue.clear()
    g.current = None
    g.loop    = False
    g.paused  = False
    # Thoát Voice Chat (không out group)
    try:
        await calls.leave_call(msg.chat.id)
        log.info("Userbot left VC in %d", msg.chat.id)
    except Exception as e:
        log.warning("leave VC error: %s", e)
    if g.np_msg:
        try:
            await client.delete_messages(msg.chat.id, g.np_msg)
        except Exception:
            pass
        g.np_msg = 0
    await msg.reply("⏹ Đã dừng và thoát VC.")

@app.on_message(filters.command("pause") & filters.group)
async def cmd_pause(client: Client, msg: Message):
    g = st(msg.chat.id)
    if not g.current:
        await msg.reply("❌ Không có bài nào đang phát.")
        return
    try:
        await calls.pause(msg.chat.id)
        g.paused = True
        await msg.reply("⏸ Tạm dừng.")
        await _update_np(client, msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ Lỗi: {e}")

@app.on_message(filters.command("resume") & filters.group)
async def cmd_resume(client: Client, msg: Message):
    g = st(msg.chat.id)
    if not g.current:
        await msg.reply("❌ Không có bài nào đang phát.")
        return
    try:
        await calls.resume(msg.chat.id)
        g.paused = False
        await msg.reply("▶️ Tiếp tục phát.")
        await _update_np(client, msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ Lỗi: {e}")

@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    g = st(msg.chat.id)
    if not g.current and not g.queue:
        await msg.reply("📋 Hàng chờ trống.")
        return
    lines = []
    if g.current:
        lines.append(f"🎵 **Đang phát:** {g.current.title} `[{_fmt(g.current.duration)}]`")
    for i, t in enumerate(g.queue, 1):
        lines.append(f"`{i}.` {t.title} `[{_fmt(t.duration)}]` — {t.requester}")
    await msg.reply("\n".join(lines))

@app.on_message(filters.command("np") & filters.group)
async def cmd_np(client: Client, msg: Message):
    g = st(msg.chat.id)
    if not g.current:
        await msg.reply("❌ Không có bài nào đang phát.")
        return
    await _send_np(client, msg.chat.id, g.current)

@app.on_message(filters.command("loop") & filters.group)
async def cmd_loop(client: Client, msg: Message):
    g = st(msg.chat.id)
    g.loop = not g.loop
    await msg.reply(f"Lặp lại: **{'BẬT 🔁' if g.loop else 'TẮT ➡️'}**")
    await _update_np(client, msg.chat.id)

@app.on_message(filters.command("volume") & filters.group)
async def cmd_volume(client: Client, msg: Message):
    g = st(msg.chat.id)
    args = msg.command[1:]
    if not args or not args[0].isdigit():
        await msg.reply(f"🔊 Âm lượng hiện tại: {g.volume}%\nDùng: /volume 80")
        return
    vol = max(0, min(200, int(args[0])))
    g.volume = vol
    try:
        await calls.change_volume_call(msg.chat.id, vol)
        log.info("Volume set to %d in %d", vol, msg.chat.id)
    except Exception as ve:
        log.warning("volume error: %s", ve)
        await msg.reply(f"⚠️ Không chỉnh được volume: {ve}")
    await msg.reply(f"🔊 Âm lượng: **{vol}%**")
    await _update_np(client, msg.chat.id)

@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    g = st(msg.chat.id)
    n = len(g.queue)
    g.queue.clear()
    await msg.reply(f"🗑 Đã xoá **{n}** bài khỏi hàng chờ.")

# ══════════════════════════════════════════════════
#  Callback query handler
# ══════════════════════════════════════════════════
@app.on_callback_query()
async def on_cb(client: Client, cb: CallbackQuery):
    data = cb.data
    cid  = cb.message.chat.id
    g    = st(cid)

    # ── Chọn bài từ kết quả tìm kiếm ──────────────
    if data.startswith("pick|"):
        _, src, idx_s = data.split("|")
        idx    = int(idx_s)
        tracks = _cache.get(cb.message.id, [])
        if not tracks or idx >= len(tracks):
            await cb.answer("❌ Hết hạn, tìm lại nhé!", show_alert=True)
            return
        track = tracks[idx]
        _cache.pop(cb.message.id, None)
        await cb.message.delete()
        await cb.answer(f"✅ {track.title[:25]}")
        if g.current:
            g.queue.append(track)
            await client.send_message(
                cid, f"➕ **{track.title}** → hàng chờ #{len(g.queue)}"
            )
        else:
            g.queue.append(track)
            await _play_next(client, cid)
        return

    if data == "pick_cancel":
        _cache.pop(cb.message.id, None)
        await cb.message.delete()
        await cb.answer("Đã huỷ.")
        return

    # ── Pause / Resume ─────────────────────────────
    if data == "vc_pause":
        try:
            if g.paused:
                await calls.resume(cid)
                g.paused = False
                await cb.answer("▶️ Tiếp tục phát")
            else:
                await calls.pause(cid)
                g.paused = True
                await cb.answer("⏸ Tạm dừng")
            await _update_np(client, cid)
        except Exception as e:
            await cb.answer(f"Lỗi: {e}", show_alert=True)
        return

    # ── Skip ───────────────────────────────────────
    if data == "vc_skip":
        if not g.current:
            await cb.answer("Không có bài nào đang phát")
            return

        user_id = cb.from_user.id
        if g.current.requester_id != user_id:
            await cb.answer(
                f"❌ Chỉ {g.current.requester} mới được skip!",
                show_alert=True
            )
            return

        await cb.answer(f"⏭ {g.current.title[:20]}")
        g.loop = False
        try:
            await calls.leave_call(cid)
        except Exception:
            pass
        await _play_next(client, cid)
        return

    # ── Stop ───────────────────────────────────────
    if data == "vc_stop":
        g.queue.clear()
        g.current = None
        g.loop    = False
        g.paused  = False
        try:
            await calls.leave_call(cid)
            log.info("Userbot left VC in %d (stop button)", cid)
        except Exception as e:
            log.warning("leave VC error: %s", e)
        try:
            await cb.message.delete()
        except Exception:
            pass
        g.np_msg = 0
        await cb.answer("⏹ Đã dừng")
        return

    # ── Loop ───────────────────────────────────────
    if data == "vc_loop":
        g.loop = not g.loop
        await cb.answer("🔁 Loop BẬT" if g.loop else "➡️ Loop TẮT")
        await _update_np(client, cid)
        return

    # ── Volume ─────────────────────────────────────
    if data == "vc_vup":
        g.volume = min(200, g.volume + 20)
        try:
            await calls.change_volume_call(cid, g.volume)
        except Exception as ve:
            log.warning("vol+ error: %s", ve)
        await cb.answer(f"🔊 {g.volume}%")
        await _update_np(client, cid)
        return

    if data == "vc_vdown":
        g.volume = max(0, g.volume - 20)
        try:
            await calls.change_volume_call(cid, g.volume)
        except Exception as ve:
            log.warning("vol- error: %s", ve)
        await cb.answer(f"🔉 {g.volume}%")
        await _update_np(client, cid)
        return

    if data == "vc_vol":
        await cb.answer(f"🔊 Âm lượng hiện tại: {g.volume}%", show_alert=True)
        return

    # ── Queue preview ──────────────────────────────
    if data == "vc_queue":
        if not g.queue:
            await cb.answer("📋 Hàng chờ trống!", show_alert=True)
        else:
            lines = [f"{i}. {t.title[:35]}" for i, t in enumerate(g.queue, 1)]
            await cb.answer("\n".join(lines[:10]), show_alert=True)
        return

# ══════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════
async def _start():
    """Khởi động tất cả clients."""
    log.info("Đang đăng nhập userbot...")
    if not userbot.is_connected:
        await userbot.start()
    ub_me = await userbot.get_me()
    log.info("✅ Userbot: %s (@%s)", ub_me.first_name, ub_me.username or "no username")

    for attempt in range(5):
        try:
            if not bot.is_connected:
                await bot.start()
            break
        except Exception as e:
            err = str(e)
            if "FLOOD_WAIT" in err:
                import re as _re
                m = _re.search(r"wait of (\d+)", err)
                wait = int(m.group(1)) if m else 60
                log.warning("FloodWait: chờ %d giây...", wait)
                await asyncio.sleep(min(wait, 300))
            else:
                raise

    bot_me = await bot.get_me()
    log.info("✅ Bot: @%s", bot_me.username)
    await calls.start()
    log.info("✅ PyTgCalls sẵn sàng — Bot đang chạy!")

async def main():
    log.info("Đang khởi động Voice Chat Bot…")
    await _start()

    # Keepalive loop — tự reconnect khi mất kết nối
    while True:
        try:
            await idle()
        except Exception as e:
            log.error("Connection lost: %s — reconnecting in 10s...", e)
            await asyncio.sleep(10)
            try:
                await _start()
                log.info("✅ Reconnected!")
            except Exception as re_err:
                log.error("Reconnect failed: %s — retry in 30s...", re_err)
                await asyncio.sleep(30)

if __name__ == "__main__":
    while True:
        try:
            _loop.run_until_complete(main())
        except KeyboardInterrupt:
            log.info("Bot dừng bởi người dùng.")
            break
        except Exception as e:
            log.error("Bot crashed: %s — restarting in 15s...", e)
            import time; time.sleep(15)

