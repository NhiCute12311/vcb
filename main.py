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

# Raw API cho Telegram Live Stream (RTMP)
from pyrogram.raw.functions.phone import (
    CreateGroupCall, GetGroupCallStreamRtmpUrl, GetGroupCall, DiscardGroupCall
)
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

# Admin tối cao — có mọi quyền (skip bất kỳ bài nào, quản lý whitelist)
ADMIN_USERNAME = "neweixyz"   # không có @

# Whitelist: tập user_id được skip bất kỳ bài nào (như admin)
_whitelist: dict[int, set] = {}
# Banlist: tập user_id bị cấm dùng bot
_banlist: dict[int, set] = {}
# RTMP link mặc định cho mỗi group: {chat_id: url}
_rtmp_default: dict[int, str] = {}
# Group nào đang bật chế độ RTMPS (stream video)
_rtmps_mode: set = set()
# RTMP livestream info: {chat_id: {"url":..., "key":..., "proc": ffmpeg_process}}
_rtmp_live: dict[int, dict] = {}

async def _rtmp_create_live(client, chat_id):
    """Tạo Telegram Live Stream (RTMP) và lấy url+key."""
    peer = await client.resolve_peer(chat_id)
    # Tạo group call ở chế độ RTMP
    try:
        await client.invoke(CreateGroupCall(
            peer=peer,
            random_id=client.rnd_id() % 2147483647,
            rtmp_stream=True,
        ))
    except Exception as e:
        # Có thể đã có call rồi — bỏ qua
        if "already" not in str(e).lower() and "GROUPCALL_INVALID" not in str(e):
            log.warning("CreateGroupCall: %s", e)
    # Lấy url + key
    res = await client.invoke(GetGroupCallStreamRtmpUrl(peer=peer, revoke=False))
    return res.url, res.key

def _rtmp_push(video_url: str, audio_url: str, rtmp_url: str, rtmp_key: str):
    """Push video+audio lên RTMP server Telegram. Ghi log ffmpeg để debug."""
    import subprocess
    target = rtmp_url.rstrip("/") + "/" + rtmp_key

    rc = ["-reconnect", "1", "-reconnect_streamed", "1",
          "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]

    if audio_url and audio_url != video_url:
        # 2 input riêng: video-only + audio-only → merge
        cmd = [
            "ffmpeg", "-re", *rc, "-i", video_url,
            "-re", *rc, "-i", audio_url,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-level", "3.1",
            "-b:v", "2000k", "-maxrate", "2000k", "-bufsize", "4000k",
            "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-keyint_min", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-f", "flv", "-flvflags", "no_duration_filesize",
            target,
        ]
    else:
        # 1 input muxed (có sẵn cả video+audio)
        cmd = [
            "ffmpeg", "-re", *rc, "-i", video_url,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-level", "3.1",
            "-b:v", "2000k", "-maxrate", "2000k", "-bufsize", "4000k",
            "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-keyint_min", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-f", "flv", "-flvflags", "no_duration_filesize",
            target,
        ]
    log.info("FFmpeg push → %s", target[:55])
    # Ghi log ffmpeg ra file để debug
    logf = open("/tmp/ffmpeg_rtmp.log", "w")
    return subprocess.Popen(cmd, stdout=logf, stderr=logf)

def _rtmp_stop(chat_id):
    """Dừng ffmpeg process của group."""
    info = _rtmp_live.get(chat_id)
    if info and info.get("proc"):
        try:
            info["proc"].terminate()
        except Exception:
            pass
        info["proc"] = None

def _get_wl(cid: int) -> set:
    if cid not in _whitelist:
        _whitelist[cid] = set()
    return _whitelist[cid]

def _get_ban(cid: int) -> set:
    if cid not in _banlist:
        _banlist[cid] = set()
    return _banlist[cid]

def _is_banned(cid: int, user_id: int, username: str = "") -> bool:
    # Admin tối cao không bao giờ bị ban
    if username and username.lower() == ADMIN_USERNAME.lower():
        return False
    return user_id in _get_ban(cid)

async def _is_privileged(client, cid: int, user_id: int, username: str = "") -> bool:
    """True nếu là admin tối cao hoặc trong whitelist."""
    # Admin tối cao theo username
    if username and username.lower() == ADMIN_USERNAME.lower():
        return True
    # Trong whitelist của group
    if user_id in _get_wl(cid):
        return True
    return False

# ══════════════════════════════════════════════════

async def _can_skip(client, cid: int, user_id: int, username: str, current_requester_id: int) -> bool:
    """True nếu được phép skip: là người chọn bài, admin, hoặc whitelist."""
    # Admin tối cao
    if username and username.lower() == ADMIN_USERNAME.lower():
        return True
    # Người chọn bài hiện tại
    if current_requester_id and current_requester_id == user_id:
        return True
    # Trong whitelist
    if user_id in _get_wl(cid):
        return True
    return False

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
    requester_id: int  = 0
    source:       str  = "yt"
    is_video:     bool = False
    is_direct:    bool = False   # True = link stream trực tiếp (m3u8/rtmp/mp4), bỏ qua yt-dlp

@dataclass
class GState:
    queue:       deque           = field(default_factory=deque)
    current:     Optional[Track] = None
    loop:        bool            = False
    paused:      bool            = False
    np_msg:      int             = 0
    tmp_file:    str             = ""
    is_playing:  bool            = False
    _play_start: float           = 0.0

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

# Các client để thử lần lượt khi 1 cái bị YouTube chặn
_CLIENT_SETS = [
    ["android"],
    ["ios"],
    ["tv_embedded"],
    ["web_safari"],
    ["android", "web"],
    ["mweb"],
]

def _fresh_cookie_copy():
    """yt-dlp GHI ĐÈ cookiefile sau mỗi request → làm hỏng cookies gốc.
    Giải pháp: mỗi lần tạo 1 bản copy tạm, yt-dlp chỉ phá bản copy,
    cookies gốc (cookies.txt) luôn nguyên vẹn."""
    if not _COOKIES_FILE or not os.path.exists(_COOKIES_FILE):
        return None
    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "ck_work.txt")
        _shutil.copy2(_COOKIES_FILE, tmp)
        return tmp
    except Exception as e:
        log.warning("Không copy được cookies: %s", e)
        return _COOKIES_FILE

def _yt_opts(extra: dict = {}, clients=None) -> dict:
    opts = {
        "quiet":        True,
        "no_warnings":  True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": clients or ["android", "web"],
            }
        },
        **extra
    }
    # Dùng BẢN COPY của cookies, không dùng file gốc
    ck = _fresh_cookie_copy()
    if ck:
        opts["cookiefile"] = ck
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

def _search_sc(query: str, n: int = 5) -> list[Track]:
    """Tìm trên SoundCloud qua yt-dlp scsearch."""
    opts = _yt_opts({"extract_flat": True})
    if _is_url(query):
        with yt_dlp.YoutubeDL(_yt_opts()) as ydl:
            info = ydl.extract_info(query, download=False)
            if info:
                return [Track(
                    title=info.get("title", "?"),
                    url=query,
                    duration=int(info.get("duration") or 0),
                    thumbnail=info.get("thumbnail", ""),
                    source="sc",
                )]
        return []
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"scsearch{n}:{query}", download=False)
    out = []
    for e in (res.get("entries") or [])[:n]:
        if not e:
            continue
        out.append(Track(
            title=e.get("title", "?"),
            url=e.get("url") or e.get("webpage_url") or "",
            duration=int(e.get("duration") or 0),
            thumbnail=e.get("thumbnail", ""),
            source="sc",
        ))
    return out

import tempfile, glob

def _extract_audio_url(info) -> str:
    formats = info.get("formats", [])
    for f in reversed(formats):
        if f.get("acodec","none") != "none" and f.get("vcodec","none") == "none" and f.get("url"):
            log.info("Audio stream: ext=%s abr=%s", f.get("ext"), f.get("abr"))
            return f["url"]
    for f in reversed(formats):
        if f.get("acodec","none") != "none" and f.get("url"):
            log.info("Muxed stream: ext=%s", f.get("ext"))
            return f["url"]
    if info.get("url"):
        return info["url"]
    return ""

def _get_stream_url(track: Track) -> str:
    """Lấy direct stream URL — thử nhiều client cho tới khi được."""
    last_err = None
    for clients in _CLIENT_SETS:
        try:
            opts = _yt_opts({"format": "bestaudio/best", "check_formats": False}, clients=clients)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(track.url, download=False)
                url = _extract_audio_url(info)
                if url:
                    log.info("OK với client=%s", clients)
                    return url
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "sign in" in msg or "bot" in msg or "403" in msg or "forbidden" in msg:
                log.warning("Client %s bị chặn, thử client khác...", clients)
                continue
            else:
                log.warning("Client %s lỗi: %s", clients, e)
                continue
    # Tất cả client bị chặn = cookies hết hạn hoặc IP bị block
    raise Exception("COOKIES_EXPIRED")

def _pick_video(info):
    formats = info.get("formats", [])
    audio_url = None
    video_url = None
    # Audio only — chất lượng cao
    for f in reversed(formats):
        if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none" and f.get("url"):
            audio_url = f["url"]
            break
    # Video 720p để nét (mp4 ưu tiên)
    best_h = 0
    for f in formats:
        h = f.get("height") or 0
        if f.get("vcodec", "none") != "none" and f.get("acodec", "none") == "none" and h <= 720 and f.get("url"):
            if h > best_h:
                best_h = h
                video_url = f["url"]
    if not video_url:
        for f in reversed(formats):
            if f.get("vcodec", "none") != "none" and f.get("url"):
                video_url = f["url"]
                break
    if not audio_url:
        for f in reversed(formats):
            if f.get("acodec", "none") != "none" and f.get("url"):
                audio_url = f["url"]
                break
    return audio_url, video_url

def _get_video_urls(track: Track):
    """Lấy URL video+audio — thử nhiều client, 720p cho nét."""
    last_err = None
    for clients in _CLIENT_SETS:
        try:
            opts = _yt_opts({
                "check_formats": False,
                "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"},
            }, clients=clients)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(track.url, download=False)
                audio_url, video_url = _pick_video(info)
                if video_url and audio_url:
                    log.info("Video OK client=%s h<=720", clients)
                    return audio_url, video_url
                elif info.get("url"):
                    return info["url"], info["url"]
        except Exception as e:
            last_err = e
            continue
    raise Exception(f"Video: tất cả client bị chặn: {last_err}")

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
# Dùng session string thay vì file — hoạt động trên Railway
SESSION_STRING = os.getenv("SESSION_STRING", "")

if SESSION_STRING:
    userbot = Client(
        "vcbot_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        sleep_threshold=60,
    )
else:
    # Fallback: dùng file session (chạy local)
    userbot = Client(
        "vcbot_userbot",
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
    max_concurrent_transmissions=4,   # xử lý nhiều lệnh song song
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
        g.current    = None
        g.is_playing = False
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

    # Xoá file tạm bài trước
    prev_tmp = getattr(st(cid), "_tmp_file", "")
    if prev_tmp and os.path.isfile(prev_tmp):
        try:
            import shutil
            shutil.rmtree(os.path.dirname(prev_tmp), ignore_errors=True)
        except Exception:
            pass

    g.current = track
    g.paused  = False
    g._tmp_file = ""

    try:
        if track.is_direct:
            # Stream trực tiếp m3u8/rtmp/mp4 — không qua yt-dlp, đưa thẳng URL vào ffmpeg
            from pytgcalls.types import VideoQuality
            try:
                ms = MediaStream(
                    track.url,
                    video_parameters=VideoQuality.HD_720p,
                    ffmpeg_parameters="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 10000000",
                )
            except Exception:
                ms = MediaStream(track.url)
        elif track.is_video:
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
            stream_url = await asyncio.get_event_loop().run_in_executor(
                None, _get_stream_url, track
            )

            try:
                from pytgcalls.types import AudioQuality
                from pytgcalls.types import MediaStream as MS
                ms = MS(
                    stream_url,
                    audio_parameters=AudioQuality.HIGH,
                    ffmpeg_parameters="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                )
            except Exception:
                try:
                    ms = MediaStream(
                        stream_url,
                        ffmpeg_parameters="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    )
                except Exception:
                    ms = MediaStream(stream_url)


        import time as _time
        g._play_start = _time.time()
        g.is_playing  = True   # Set TRƯỚC khi play
        await calls.play(cid, ms)
        log.info("calls.play OK")
        await _send_np(client, cid, track)
        log.info("▶ Playing%s: %s [chat=%d]", " [VIDEO]" if track.is_video else "", track.title, cid)
    except Exception as e:
        log.error("_play_next error: %s", e)
        err_low = str(e).lower()
        if "no active" in err_low or "groupcall" in err_low or "not found" in err_low or "bot_method" in err_low or "invalid" in err_low or "no group call" in err_low:
            # VC không tồn tại/đã đóng → reset sạch, không giữ queue cũ
            await _reset_state(cid, "no active VC")
            await client.send_message(
                cid,
                "❌ Chưa có Voice Chat đang mở! Mở VC trong group rồi /play lại.\n(Đã xoá hàng chờ cũ)"
            )
        elif "connection" in err_low or "lost" in err_low or "timeout" in err_low:
            # Connection lost — thử rejoin và phát lại bài hiện tại
            log.warning("Connection lost, retrying in 5s...")
            await asyncio.sleep(5)
            try:
                stream_url = await asyncio.get_event_loop().run_in_executor(None, _get_stream_url, track)
                ms = MediaStream(stream_url, ffmpeg_parameters="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 3")
                import time as _t
                g._play_start = _t.time()
                g.is_playing = True
                await calls.play(cid, ms)
                log.info("Reconnected and replaying: %s", track.title)
            except Exception as e2:
                log.error("Retry failed: %s", e2)
                if g.queue:
                    g.current = None
                    await _play_next(client, cid)
        elif "cookies_expired" in err_low or "sign in" in err_low or "confirm" in err_low:
            # Cookies hết hạn — báo rõ và dừng, không spam queue
            await _reset_state(cid, "cookies expired")
            await client.send_message(
                cid,
                "🍪 **Cookies YouTube đã hết hạn!**\n"
                "Admin cần gửi file `cookies.txt` mới kèm lệnh `/updatecookies`.\n"
                "(Đã xoá hàng chờ để tránh lỗi)"
            )
        else:
            await client.send_message(cid, "❌ Lỗi phát bài này, bỏ qua.")
            if g.queue:
                g.current = None
                await _play_next(client, cid)

# ══════════════════════════════════════════════════
#  Stream end event handler
#  Tương thích cả version cũ lẫn mới của py-tgcalls
# ══════════════════════════════════════════════════
async def _reset_state(cid: int, reason: str = ""):
    """Xoá sạch hàng chờ + trạng thái khi VC đóng/stop."""
    g = st(cid)
    g.queue.clear()
    g.current    = None
    g.loop       = False
    g.paused     = False
    g.is_playing = False
    # Xoá file tạm
    if g.tmp_file and os.path.isfile(g.tmp_file):
        try:
            import shutil
            shutil.rmtree(os.path.dirname(g.tmp_file), ignore_errors=True)
        except Exception:
            pass
    g.tmp_file = ""
    if g.np_msg:
        try:
            await bot.delete_messages(cid, g.np_msg)
        except Exception:
            pass
        g.np_msg = 0
    log.info("State reset in %d (%s)", cid, reason)

@calls.on_update()
async def _on_update(_, update):
    try:
        cid = update.chat_id
    except AttributeError:
        return

    cls = type(update).__name__
    log.info("VC update: %s in %d", cls, cid)

    # ── VC bị đóng (người tắt voice chat) → reset toàn bộ ──
    if cls in ("ClosedVoiceChat", "GroupCallClosed", "Closed", "LeftGroupCall",
               "LeftVoiceChat", "KickedFromGroupCall"):
        log.info("VC closed (%s) in %d → reset", cls, cid)
        try:
            await calls.leave_call(cid)
        except Exception:
            pass
        await _reset_state(cid, "VC closed")
        try:
            await bot.send_message(cid, "⏹ Voice Chat đã đóng — đã xoá hàng chờ.")
        except Exception:
            pass
        return

    # ── Stream kết thúc bình thường → phát bài tiếp ──
    should_next = False
    if _HAS_STREAM_EVENTS:
        if isinstance(update, StreamAudioEnded):
            should_next = True
        elif isinstance(update, StreamVideoEnded):
            should_next = True
    if cls in ("StreamAudioEnded", "StreamVideoEnded", "StreamEnded"):
        should_next = True

    if should_next:
        import time as _time
        g = st(cid)
        elapsed = _time.time() - getattr(g, "_play_start", 0)
        log.info("StreamEnded: is_playing=%s elapsed=%.1fs", g.is_playing, elapsed)
        if g.current and g.is_playing and elapsed > 5:
            g.is_playing = False
            log.info("Stream ended → next in %d", cid)
            await _play_next(bot, cid)
        else:
            log.info("StreamEnded ignored (too soon)")

# ══════════════════════════════════════════════════
#  Search result cache + keyboard
# ══════════════════════════════════════════════════
_cache: dict[int, list[Track]] = {}

def _cache_set(mid, tracks):
    """Lưu kết quả tìm kiếm, giới hạn 50 entry để không leak RAM."""
    _cache[mid] = tracks
    if len(_cache) > 50:
        # Xoá entry cũ nhất
        oldest = next(iter(_cache))
        _cache.pop(oldest, None)

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

# Lưu query đang chờ chọn nguồn: {message_id: (query, is_video, requester, requester_id)}
_pending_src: dict[int, tuple] = {}

def _source_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ YouTube",    callback_data="src|yt"),
        InlineKeyboardButton("🎵 SoundCloud", callback_data="src|sc"),
    ], [
        InlineKeyboardButton("❌ Huỷ", callback_data="pick_cancel"),
    ]])

# ══════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════
@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    await msg.reply(
        "🎵 **Voice Chat Music Bot**\n\n"
        "`/play <tên bài>` — Phát nhạc từ YouTube\n"
        "`/video <tên>`    — Stream video YouTube vào VC\n"
        "`/playrtmp <link>`— Stream m3u8/RTMP/phim trực tiếp\n"
        "`/setrtmp <link>` — Lưu link RTMP mặc định\n"
        "`/setrtmps`       — Mở Live Stream 🔴 cho group\n"
        "`/playrtmps <tên>`— Tìm video, chọn YT/SCL, phát lên live\n"
        "`/stoprtmps`      — Dừng Live Stream\n"
        "`/skip`           — Bỏ qua bài (chỉ người chọn bài)\n"
        "`/stop`           — Dừng và thoát VC\n"
        "`/pause` `/resume`— Tạm dừng / tiếp tục\n"
        "`/queue`          — Xem hàng chờ\n"
        "`/np`             — Bài đang phát\n"
        "`/loop`           — Bật / tắt lặp lại\n"
        "`/clear`          — Xoá hàng chờ\n\n"
        "**Admin:**\n"
        "`/wl @user`       — Cho phép skip mọi bài\n"
        "`/unwl @user`     — Gỡ quyền skip\n"
        "`/wllist`         — Xem whitelist\n"
        "`/bl @user`       — Blacklist (cấm dùng bot)\n"
        "`/unbl @user`     — Gỡ blacklist\n\n"
        "⚠️ Group phải có **Voice Chat đang mở** trước khi dùng."
    )

# ── Chặn user bị ban TRƯỚC mọi lệnh khác ──────────
@app.on_message(filters.command([
    "play", "video", "stream", "rtmp", "playrtmp", "playrtmps", "setrtmps", "stoprtmps", "fflog", "skip",
    "stop", "pause", "resume", "queue", "np", "loop", "clear"
]) & filters.group, group=-2)
async def _ban_guard(client: Client, msg: Message):
    if not msg.from_user:
        return
    uid   = msg.from_user.id
    uname = msg.from_user.username or ""
    if _is_banned(msg.chat.id, uid, uname):
        try:
            await msg.reply("🚫 Bạn đã bị cấm sử dụng bot trong group này.")
        except Exception:
            pass
        msg.stop_propagation()   # chặn không cho lệnh chạy tiếp

@app.on_message(filters.command(["blacklist", "bl"]) & filters.group)
async def cmd_blacklist(client: Client, msg: Message):
    uname = msg.from_user.username if msg.from_user else ""
    if not (uname and uname.lower() == ADMIN_USERNAME.lower()):
        await msg.reply("❌ Chỉ admin mới được blacklist.")
        return
    target_id = None; target_name = ""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id   = msg.reply_to_message.from_user.id
        target_name = msg.reply_to_message.from_user.first_name
    elif len(msg.command) > 1:
        u = msg.command[1].lstrip("@")
        try:
            usr = await client.get_users(u)
            target_id = usr.id; target_name = usr.first_name
        except Exception:
            await msg.reply(f"❌ Không tìm thấy @{u}")
            return
    if not target_id:
        await msg.reply("Dùng: reply tin nhắn + `/bl`, hoặc `/bl @username`")
        return
    _get_ban(msg.chat.id).add(target_id)
    _get_wl(msg.chat.id).discard(target_id)  # ban thì gỡ whitelist
    await msg.reply(f"🚫 Đã blacklist **{target_name}** — không dùng được bot.")

@app.on_message(filters.command(["unblacklist", "unbl"]) & filters.group)
async def cmd_unblacklist(client: Client, msg: Message):
    uname = msg.from_user.username if msg.from_user else ""
    if not (uname and uname.lower() == ADMIN_USERNAME.lower()):
        await msg.reply("❌ Chỉ admin mới được gỡ blacklist.")
        return
    target_id = None; target_name = ""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id   = msg.reply_to_message.from_user.id
        target_name = msg.reply_to_message.from_user.first_name
    elif len(msg.command) > 1:
        u = msg.command[1].lstrip("@")
        try:
            usr = await client.get_users(u)
            target_id = usr.id; target_name = usr.first_name
        except Exception:
            await msg.reply(f"❌ Không tìm thấy @{u}")
            return
    if not target_id:
        await msg.reply("Dùng: reply tin nhắn + `/unbl`, hoặc `/unbl @username`")
        return
    _get_ban(msg.chat.id).discard(target_id)
    await msg.reply(f"✅ Đã gỡ blacklist **{target_name}**.")

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
    _cache_set(m.id, tracks)

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
    _cache_set(m.id, tracks)

def _stream_title(url: str) -> str:
    low = url.lower()
    if ".m3u8" in low:
        return "📡 Live Stream (HLS)"
    if low.startswith("rtmp"):
        return "📡 RTMP Stream"
    if any(ext in low for ext in [".mp4", ".mkv", ".ts", ".avi", ".webm"]):
        return "🎬 Video Stream"
    return "📡 Direct Stream"

def _valid_stream_url(url: str) -> bool:
    low = url.lower()
    return (low.startswith("http://") or low.startswith("https://")
            or low.startswith("rtmp://") or low.startswith("rtmps://"))

async def _do_stream(client: Client, msg: Message, url: str):
    if not _valid_stream_url(url):
        await msg.reply("❌ Link không hợp lệ. Phải bắt đầu bằng http://, https://, rtmp:// hoặc rtmps://")
        return
    requester    = msg.from_user.first_name if msg.from_user else "?"
    requester_id = msg.from_user.id if msg.from_user else 0
    track = Track(
        title=_stream_title(url),
        url=url,
        duration=0,
        requester=requester,
        requester_id=requester_id,
        is_video=True,
        is_direct=True,
    )
    g = st(msg.chat.id)
    s = await msg.reply("📡 Đang kết nối stream...")
    if g.current:
        g.queue.append(track)
        await s.edit(f"➕ Đã thêm stream vào hàng chờ #{len(g.queue)}")
    else:
        g.queue.append(track)
        await s.delete()
        await _play_next(client, msg.chat.id)

@app.on_message(filters.command(["stream", "rtmp", "playrtmp"]) & filters.group)
async def cmd_stream(client: Client, msg: Message):
    """Stream link m3u8/HLS/RTMP/RTMPS/mp4 vào VC. Không nhập link → dùng link đã /setrtmp."""
    url = " ".join(msg.command[1:]).strip()
    if not url:
        # Dùng link mặc định đã set
        url = _rtmp_default.get(msg.chat.id, "")
        if not url:
            await msg.reply(
                "❓ Dùng: `/playrtmp <link>`\n\n"
                "Hỗ trợ: m3u8/HLS, RTMP, RTMPS, mp4/mkv/ts\n"
                "VD: `/playrtmp https://example.com/live.m3u8`\n\n"
                "Hoặc lưu link mặc định bằng `/setrtmp <link>` rồi chỉ cần gõ `/playrtmp`."
            )
            return
        await msg.reply(f"📡 Dùng link RTMP đã lưu...")
    await _do_stream(client, msg, url)

@app.on_message(filters.command(["setrtmp"]) & filters.group)
async def cmd_setrtmp(client: Client, msg: Message):
    """Lưu link RTMP mặc định cho group."""
    url = " ".join(msg.command[1:]).strip()
    if not url:
        cur = _rtmp_default.get(msg.chat.id, "")
        if cur:
            await msg.reply(f"📡 Link RTMP hiện tại:\n`{cur}`\n\nĐổi: `/setrtmp <link mới>`\nXoá: `/setrtmp clear`")
        else:
            await msg.reply("❓ Dùng: `/setrtmp <link>` để lưu link RTMP mặc định.")
        return
    if url.lower() == "clear":
        _rtmp_default.pop(msg.chat.id, None)
        await msg.reply("🗑 Đã xoá link RTMP mặc định.")
        return
    if not _valid_stream_url(url):
        await msg.reply("❌ Link không hợp lệ.")
        return
    _rtmp_default[msg.chat.id] = url
    await msg.reply(f"✅ Đã lưu link RTMP mặc định!\nGõ `/playrtmp` (không cần link) để phát.")

@app.on_message(filters.command(["setrtmps"]) & filters.group)
async def cmd_setrtmps(client: Client, msg: Message):
    """Mở Telegram Live Stream (RTMP) cho group — chuẩn bị phát."""
    cid = msg.chat.id
    s = await msg.reply("📡 Đang mở Live Stream cho group...")
    try:
        # userbot tạo livestream (cần là owner/admin có quyền)
        url, key = await _rtmp_create_live(userbot, cid)
        _rtmp_live[cid] = {"url": url, "key": key, "proc": None}
        _rtmps_mode.add(cid)
        await s.edit(
            "✅ **Đã mở Live Stream!** 🔴\n"
            "Mọi người trong group bấm vào livestream để xem.\n\n"
            "Giờ dùng `/playrtmps <tên>` để tìm video và phát lên live."
        )
    except Exception as e:
        err = str(e)
        if "rtmp_stream" in err.lower() or "admin" in err.lower() or "CHAT_ADMIN" in err:
            await s.edit("❌ Userbot cần là **chủ group** hoặc admin có quyền quản lý livestream.")
        else:
            await s.edit(f"❌ Lỗi mở live: {err}")

@app.on_message(filters.command(["playrtmps"]) & filters.group)
async def cmd_playrtmps(client: Client, msg: Message):
    """Search video để stream — hiện 2 nút chọn nguồn YouTube/SoundCloud."""
    q = " ".join(msg.command[1:]).strip()
    if not q:
        await msg.reply("❓ Dùng: `/playrtmps <tên video>`\nVD: `/playrtmps lofi hip hop`")
        return
    cid = msg.chat.id
    if cid not in _rtmp_live:
        await msg.reply("❌ Chưa mở Live Stream! Gõ `/setrtmps` trước.")
        return
    requester    = msg.from_user.first_name if msg.from_user else "?"
    requester_id = msg.from_user.id if msg.from_user else 0
    m = await msg.reply(
        f"🔍 **{q}**\nChọn nguồn để tìm video:",
        reply_markup=_source_kb(),
    )
    # tuple thứ 5 = True nghĩa là chế độ RTMPS (push live)
    _pending_src[m.id] = (q, True, requester, requester_id, True)

@app.on_message(filters.command(["stoprtmps"]) & filters.group)
async def cmd_stoprtmps(client: Client, msg: Message):
    """Dừng Live Stream RTMP."""
    cid = msg.chat.id
    _rtmp_stop(cid)          # dừng ffmpeg
    _rtmp_live.pop(cid, None)
    _rtmps_mode.discard(cid)
    await msg.reply("⏹ Đã dừng Live Stream RTMP.\n(Để đóng hẳn livestream, dùng nút Telegram trong group)")

@app.on_message(filters.command(["fflog"]) & filters.group)
async def cmd_fflog(client: Client, msg: Message):
    """Xem log ffmpeg để debug RTMP."""
    try:
        with open("/tmp/ffmpeg_rtmp.log", encoding="utf-8", errors="ignore") as f:
            data = f.read()
        # Lấy 1500 ký tự cuối
        tail = data[-1500:] if len(data) > 1500 else data
        if not tail.strip():
            tail = "(log trống — ffmpeg chưa chạy hoặc chưa ghi gì)"
        await msg.reply(f"```\n{tail}\n```")
    except FileNotFoundError:
        await msg.reply("Chưa có log ffmpeg.")
    except Exception as e:
        await msg.reply(f"Lỗi đọc log: {e}")

@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(client: Client, msg: Message):
    g = st(msg.chat.id)
    if not g.current:
        await msg.reply("❌ Không có bài nào đang phát.")
        return

    user_id  = msg.from_user.id if msg.from_user else 0
    username = msg.from_user.username if msg.from_user else ""

    allowed = await _can_skip(client, msg.chat.id, user_id, username, g.current.requester_id)
    log.info("Skip cmd by %s(id=%d): allowed=%s (requester=%d, wl=%s)",
             username or user_id, user_id, allowed, g.current.requester_id, user_id in _get_wl(msg.chat.id))
    if not allowed:
        await msg.reply(f"❌ Chỉ **{g.current.requester}** (người chọn bài), admin, hoặc whitelist mới được skip!")
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

@app.on_message(filters.command(["whitelist", "wl"]) & filters.group)
async def cmd_whitelist(client: Client, msg: Message):
    """Admin thêm người vào whitelist (được skip mọi bài). Reply tin nhắn của họ hoặc tag @user."""
    user_id  = msg.from_user.id if msg.from_user else 0
    username = msg.from_user.username if msg.from_user else ""

    # Chỉ admin tối cao mới được quản lý whitelist
    if not (username and username.lower() == ADMIN_USERNAME.lower()):
        await msg.reply("❌ Chỉ admin mới được quản lý whitelist.")
        return

    target_id   = None
    target_name = ""
    # Cách 1: reply tin nhắn
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id   = msg.reply_to_message.from_user.id
        target_name = msg.reply_to_message.from_user.first_name
    # Cách 2: tag username
    elif len(msg.command) > 1:
        uname = msg.command[1].lstrip("@")
        try:
            u = await client.get_users(uname)
            target_id   = u.id
            target_name = u.first_name
        except Exception:
            await msg.reply(f"❌ Không tìm thấy user @{uname}")
            return

    if not target_id:
        await msg.reply("Dùng: reply tin nhắn của người đó + `/wl`, hoặc `/wl @username`")
        return

    _get_wl(msg.chat.id).add(target_id)
    await msg.reply(f"✅ Đã thêm **{target_name}** vào whitelist — giờ có thể skip mọi bài.")

@app.on_message(filters.command(["unwhitelist", "unwl"]) & filters.group)
async def cmd_unwhitelist(client: Client, msg: Message):
    user_id  = msg.from_user.id if msg.from_user else 0
    username = msg.from_user.username if msg.from_user else ""
    if not (username and username.lower() == ADMIN_USERNAME.lower()):
        await msg.reply("❌ Chỉ admin mới được quản lý whitelist.")
        return

    target_id = None
    target_name = ""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id   = msg.reply_to_message.from_user.id
        target_name = msg.reply_to_message.from_user.first_name
    elif len(msg.command) > 1:
        uname = msg.command[1].lstrip("@")
        try:
            u = await client.get_users(uname)
            target_id   = u.id
            target_name = u.first_name
        except Exception:
            await msg.reply(f"❌ Không tìm thấy user @{uname}")
            return

    if not target_id:
        await msg.reply("Dùng: reply tin nhắn + `/unwl`, hoặc `/unwl @username`")
        return

    _get_wl(msg.chat.id).discard(target_id)
    await msg.reply(f"✅ Đã xoá **{target_name}** khỏi whitelist.")

@app.on_message(filters.command("wllist") & filters.group)
async def cmd_wllist(client: Client, msg: Message):
    wl = _get_wl(msg.chat.id)
    if not wl:
        await msg.reply("📋 Whitelist trống.")
        return
    names = []
    for uid in wl:
        try:
            u = await client.get_users(uid)
            names.append(f"• {u.first_name} (@{u.username or uid})")
        except Exception:
            names.append(f"• {uid}")
    await msg.reply("📋 **Whitelist:**\n" + "\n".join(names))

@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    g = st(msg.chat.id)
    n = len(g.queue)
    g.queue.clear()
    await msg.reply(f"Xoa {n} bai khoi hang cho.")

@app.on_message(filters.command("updatecookies"))
async def cmd_update_cookies(client: Client, msg: Message):
    if not msg.document:
        await msg.reply("Gui file cookies.txt kem lenh /updatecookies de cap nhat khi YouTube bi block.")
        return
    if not msg.document.file_name.endswith(".txt"):
        await msg.reply("File phai la .txt")
        return
    s = await msg.reply("Dang cap nhat cookies...")
    try:
        await client.download_media(msg.document, file_name="cookies.txt")
        global _COOKIES_FILE
        _COOKIES_FILE = "cookies.txt"
        log.info("Cookies updated")
        await s.edit("Cookies da cap nhat! YouTube se hoat dong tro lai.")
    except Exception as e:
        await s.edit(f"Loi: {e}")

@app.on_callback_query()
async def on_cb(client: Client, cb: CallbackQuery):
    data = cb.data
    cid  = cb.message.chat.id
    g    = st(cid)

    # Chặn user bị blacklist
    bl_uname = cb.from_user.username or ""
    if _is_banned(cid, cb.from_user.id, bl_uname):
        await cb.answer("🚫 Bạn đã bị blacklist, không dùng được bot.", show_alert=True)
        return

    # ── Chọn bài từ kết quả tìm kiếm ──────────────
    # ── Chọn nguồn YouTube/SoundCloud cho /playrtmps ──
    if data.startswith("src|"):
        source = data.split("|")[1]   # "yt" hoặc "sc"
        pending = _pending_src.get(cb.message.id)
        if not pending:
            await cb.answer("❌ Hết hạn, tìm lại nhé!", show_alert=True)
            return
        query        = pending[0]
        is_video     = pending[1]
        requester    = pending[2]
        requester_id = pending[3]
        is_rtmps     = pending[4] if len(pending) > 4 else False
        _pending_src.pop(cb.message.id, None)
        src_name = "YouTube" if source == "yt" else "SoundCloud"
        await cb.answer(f"🔍 Tìm trên {src_name}...")
        await cb.message.edit_text(f"🔍 Đang tìm **{query}** trên {src_name}...")
        try:
            fn = _search_yt if source == "yt" else _search_sc
            tracks = await asyncio.get_event_loop().run_in_executor(None, fn, query, 5)
        except Exception as e:
            await cb.message.edit_text(f"❌ Lỗi tìm kiếm: {e}")
            return
        if not tracks:
            await cb.message.edit_text("😔 Không tìm thấy kết quả.")
            return
        for t in tracks:
            t.requester    = requester
            t.requester_id = requester_id
            t.is_video     = is_video
        # Prefix callback để biết đây là RTMPS hay VC
        kb_src = ("rtmps_" + source) if is_rtmps else source
        await cb.message.edit_text(
            f"🎬 **{query}** ({src_name}) — chọn:",
            reply_markup=_search_kb(tracks, kb_src),
        )
        _cache_set(cb.message.id, tracks)
        return

    if data.startswith("pick|"):
        _, src, idx_s = data.split("|")
        idx    = int(idx_s)
        tracks = _cache.get(cb.message.id, [])
        if not tracks or idx >= len(tracks):
            await cb.answer("❌ Hết hạn, tìm lại nhé!", show_alert=True)
            return
        track = tracks[idx]
        _cache.pop(cb.message.id, None)

        # ── Chế độ RTMPS: push lên livestream qua ffmpeg ──
        if src.startswith("rtmps_"):
            await cb.message.delete()
            await cb.answer(f"📡 Đang phát lên live: {track.title[:25]}")
            info = _rtmp_live.get(cid)
            if not info:
                await client.send_message(cid, "❌ Live Stream chưa mở. Gõ /setrtmps trước.")
                return
            st_msg = await client.send_message(cid, f"📡 Đang lấy video **{track.title}**...")
            try:
                # Lấy URL video+audio để push
                audio_url, video_url = await asyncio.get_event_loop().run_in_executor(
                    None, _get_video_urls, track
                )
                # Dừng ffmpeg cũ nếu đang chạy
                _rtmp_stop(cid)
                # Push lên RTMP với cả video + audio
                proc = _rtmp_push(video_url, audio_url, info["url"], info["key"])
                info["proc"] = proc
                await st_msg.edit(
                    f"🔴 **ĐANG PHÁT LIVE:** {track.title}\n"
                    f"👤 {track.requester}\n"
                    f"Mọi người mở livestream của group để xem!"
                )
            except Exception as e:
                await st_msg.edit(f"❌ Lỗi push live: {e}")
            return

        # ── Chế độ VC bình thường ──
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
        _pending_src.pop(cb.message.id, None)
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

        user_id  = cb.from_user.id
        username = cb.from_user.username or ""
        allowed  = await _can_skip(client, cid, user_id, username, g.current.requester_id)
        log.info("Skip btn by %s(id=%d): allowed=%s (requester=%d, wl=%s)",
                 username or user_id, user_id, allowed, g.current.requester_id, user_id in _get_wl(cid))
        if not allowed:
            await cb.answer(f"❌ Chỉ {g.current.requester} (người chọn bài), admin, hoặc whitelist mới được skip!", show_alert=True)
            return

        await cb.answer(f"⏭ {g.current.title[:20]}")
        g.loop = False
        g.current = None
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

async def _watchdog():
    """Ping Telegram mỗi 60s để giữ kết nối."""
    while True:
        await asyncio.sleep(60)
        try:
            await userbot.get_me()
            await bot.get_me()
        except Exception as e:
            log.warning("Watchdog: kết nối yếu (%s), thử reconnect...", e)
            try:
                if not userbot.is_connected:
                    await userbot.start()
                if not bot.is_connected:
                    await bot.start()
                log.info("Watchdog: reconnected OK")
            except Exception as re:
                log.error("Watchdog reconnect failed: %s", re)

async def main():
    log.info("Đang khởi động Voice Chat Bot…")
    await _start()

    # Chạy watchdog ngầm
    asyncio.create_task(_watchdog())

    # Keepalive loop
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
            err = str(e)
            log.error("Bot crashed: %s — restarting in 15s...", e)
            # Xoá session nếu bị AUTH_KEY_DUPLICATED
            if "AUTH_KEY_DUPLICATED" in err or "auth_key" in err.lower():
                log.warning("AUTH_KEY_DUPLICATED — xoá session và đăng nhập lại...")
                for f in ["vcbot_userbot.session", "vcbot_userbot.session-journal",
                          "vcbot_bot.session", "vcbot_bot.session-journal"]:
                    try:
                        os.remove(f)
                        log.info("Đã xoá: %s", f)
                    except Exception:
                        pass
                import time; time.sleep(5)
            else:
                import time; time.sleep(15)

