#!/usr/bin/env python3
"""
Module /xemphim — Kho phim qua KKPhim API (phimapi.com)
Tích hợp vào vc_bot_final.py bằng: from phim_module import register_phim
rồi gọi register_phim(app, on_play) trong main.

on_play(client, msg, title, m3u8_url, mode) — callback do bot chính cung cấp:
  mode = "vplay" (phát vào VC) hoặc "rtmps" (livestream)
"""
import aiohttp
from pyrogram import filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

API_BASE = "https://phimapi.com"
IMG_BASE = "https://phimimg.com"

# Cache: {message_id: {"slug":..., "movie":..., "episodes":..., "server_idx":..., "mode":...}}
_film_cache: dict = {}

# ── Gọi API ───────────────────────────────────────
async def _api_get(path: str) -> dict:
    url = path if path.startswith("http") else (API_BASE + path)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()

async def api_phim_moi(page: int = 1) -> dict:
    return await _api_get(f"/danh-sach/phim-moi-cap-nhat?page={page}")

async def api_tim_kiem(keyword: str, page: int = 1, limit: int = 10) -> dict:
    from urllib.parse import quote
    return await _api_get(f"/v1/api/tim-kiem?keyword={quote(keyword)}&page={page}&limit={limit}")

async def api_chi_tiet(slug: str) -> dict:
    return await _api_get(f"/phim/{slug}")

async def api_danh_sach(loai: str, page: int = 1, limit: int = 10) -> dict:
    # loai: phim-le, phim-bo, hoat-hinh, tv-shows
    return await _api_get(f"/v1/api/danh-sach/{loai}?page={page}&limit={limit}")

# ── Keyboards ─────────────────────────────────────
def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Phim mới cập nhật", callback_data="ph|new|1")],
        [InlineKeyboardButton("🔍 Tìm Phim",          callback_data="ph|searchhint"),
         InlineKeyboardButton("📚 Danh sách phim",    callback_data="ph|list|phim-bo|1")],
        [InlineKeyboardButton("🎬 Phim lẻ",           callback_data="ph|list|phim-le|1"),
         InlineKeyboardButton("📺 Phim bộ",           callback_data="ph|list|phim-bo|1")],
        [InlineKeyboardButton("🎞 Hoạt hình",          callback_data="ph|list|hoat-hinh|1")],
    ])

def _list_kb(items: list, page: int, total_pages: int, src: str) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        slug = it.get("slug", "")
        name = it.get("name", "?")
        year = it.get("year", "")
        epc  = it.get("episode_current", "")
        label = f"{name} • {year} | {epc}" if year else name
        rows.append([InlineKeyboardButton(label[:50], callback_data=f"ph|film|{slug}")])
    # Nút phân trang
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"ph|{src}|{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="ph|noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Sau ➡️", callback_data=f"ph|{src}|{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Trang chủ", callback_data="ph|home")])
    return InlineKeyboardMarkup(rows)

def _film_kb(slug: str, episodes: list, server_idx: int, mode: str, page: int = 1) -> InlineKeyboardMarkup:
    rows = []
    if episodes:
        server = episodes[server_idx]
        eps = server.get("server_data", [])
        # Phân trang tập: 20 tập/trang
        per = 20
        start = (page - 1) * per
        chunk = eps[start:start + per]
        total_ep_pages = max(1, (len(eps) + per - 1) // per)
        row = []
        for i, ep in enumerate(chunk):
            real_idx = start + i
            row.append(InlineKeyboardButton(ep.get("name", str(real_idx+1)),
                                            callback_data=f"ph|ep|{slug}|{server_idx}|{real_idx}"))
            if len(row) == 5:
                rows.append(row); row = []
        if row:
            rows.append(row)
        # Chế độ phát
        rows.append([
            InlineKeyboardButton(("✅ " if mode=="vplay" else "") + "VPLAY", callback_data=f"ph|mode|{slug}|vplay"),
            InlineKeyboardButton(("✅ " if mode=="rtmps" else "") + "📡 RTMPS", callback_data=f"ph|mode|{slug}|rtmps"),
        ])
        # Chọn server nếu nhiều
        if len(episodes) > 1:
            srow = []
            for si, sv in enumerate(episodes):
                mark = "✅ " if si == server_idx else ""
                srow.append(InlineKeyboardButton(f"{mark}{sv.get('server_name','SV')[:12]}",
                                                 callback_data=f"ph|sv|{slug}|{si}"))
            for j in range(0, len(srow), 2):
                rows.append(srow[j:j+2])
        # Phân trang tập
        if total_ep_pages > 1:
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("⬅️", callback_data=f"ph|eppage|{slug}|{server_idx}|{page-1}"))
            nav.append(InlineKeyboardButton(f"📄 {page}/{total_ep_pages}", callback_data="ph|noop"))
            if page < total_ep_pages:
                nav.append(InlineKeyboardButton("➡️", callback_data=f"ph|eppage|{slug}|{server_idx}|{page+1}"))
            rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔙 Quay lại", callback_data="ph|new|1"),
        InlineKeyboardButton("🏠 Trang chủ", callback_data="ph|home"),
    ])
    return InlineKeyboardMarkup(rows)

# ── Format mô tả phim ─────────────────────────────
def _fmt_film_info(movie: dict) -> str:
    name      = movie.get("name", "?")
    origin    = movie.get("origin_name", "")
    year      = movie.get("year", "")
    quality   = movie.get("quality", "")
    lang      = movie.get("lang", "")
    time      = movie.get("time", "")
    status    = movie.get("episode_current", "")
    total     = movie.get("episode_total", "")
    cats      = ", ".join(c.get("name","") for c in movie.get("category", []))
    countries = ", ".join(c.get("name","") for c in movie.get("country", []))
    director  = ", ".join(movie.get("director", []) or ["?"])
    actors    = ", ".join(movie.get("actor", []) or ["?"])
    content   = movie.get("content", "").replace("<p>", "").replace("</p>", "").strip()
    if len(content) > 500:
        content = content[:500] + "..."
    return (
        f"🎬 **{name}**\n"
        f"📺 {origin} | {year} | {quality} | {lang} | {time}\n"
        f"🔴 Trạng thái: {status} / {total} tập\n"
        f"🎭 Thể loại: {cats}\n"
        f"🌍 Quốc gia: {countries}\n"
        f"🎥 Đạo diễn: {director}\n"
        f"👥 Diễn viên: {actors[:200]}\n\n"
        f"📝 **Nội dung:**\n{content}"
    )

# ── Đăng ký handlers ──────────────────────────────
def register_phim(app, on_play, ban_check=None):
    """on_play(client, chat_id, title, m3u8_url, mode) — async callback phát phim."""

    @app.on_message(filters.command("xemphim") & filters.group)
    async def cmd_xemphim(client, msg: Message):
        if ban_check and ban_check(msg.chat.id, msg.from_user.id if msg.from_user else 0,
                                    msg.from_user.username if msg.from_user else ""):
            await msg.reply("🚫 Bạn đã bị blacklist.")
            return
        s = await msg.reply("🎬 Đang tải kho phim...")
        try:
            data = await api_phim_moi(1)
            today = data.get("pagination", {}).get("totalItemsPerPage", "?")
            total = data.get("pagination", {}).get("totalItems", "?")
        except Exception as e:
            await s.edit(f"❌ Lỗi kết nối API phim: {e}")
            return
        txt = (
            "🎬 **CloudXFilm**\n"
            "📦 Kho phim khổng lồ — cập nhật mỗi ngày\n"
            "Từ bom tấn mới ra rạp đến series kinh điển ✨\n\n"
            f"📚 Tổng: **{total}** phim\n\n"
            "👇 Bạn muốn làm gì?\n"
            "• Duyệt phim theo Danh sách / Thể loại\n"
            "• Tìm kiếm nhanh theo tên\n"
            "• Chọn server & tập → trỏ /vplay hoặc /playrtmps vào nhóm"
        )
        await s.edit(txt, reply_markup=_home_kb())

    @app.on_message(filters.command("timphim") & filters.group)
    async def cmd_timphim(client, msg: Message):
        kw = " ".join(msg.command[1:]).strip()
        if not kw:
            await msg.reply("🔍 Dùng: `/timphim <tên phim>`")
            return
        s = await msg.reply(f"🔍 Đang tìm **{kw}**...")
        try:
            data = await api_tim_kiem(kw, 1, 10)
            items = data.get("data", {}).get("items", [])
        except Exception as e:
            await s.edit(f"❌ Lỗi tìm: {e}")
            return
        if not items:
            await s.edit("😔 Không tìm thấy phim.")
            return
        _film_cache[s.id] = {"search_kw": kw}
        await s.edit(f"🔍 Kết quả cho **{kw}**:", reply_markup=_list_kb(items, 1, 1, f"search|{kw}"))

    @app.on_callback_query(filters.regex(r"^ph\|"))
    async def on_phim_cb(client, cb: CallbackQuery):
        if ban_check and ban_check(cb.message.chat.id, cb.from_user.id, cb.from_user.username or ""):
            await cb.answer("🚫 Bạn đã bị blacklist.", show_alert=True)
            return
        parts = cb.data.split("|")
        action = parts[1]

        if action == "noop":
            await cb.answer()
            return

        if action == "home":
            await cb.answer()
            await cb.message.edit_text("🎬 **CloudXFilm** — chọn:", reply_markup=_home_kb())
            return

        if action == "searchhint":
            await cb.answer("Dùng lệnh /timphim <tên> để tìm", show_alert=True)
            return

        # Phim mới cập nhật
        if action == "new":
            page = int(parts[2])
            await cb.answer("Đang tải...")
            try:
                data = await api_phim_moi(page)
                items = data.get("items", [])
                pg = data.get("pagination", {})
                total_pages = pg.get("totalPages", 1)
            except Exception as e:
                await cb.answer(f"Lỗi: {e}", show_alert=True)
                return
            await cb.message.edit_text(
                f"🆕 **Phim Mới** — Trang {page}\nHiển thị {len(items)} phim",
                reply_markup=_list_kb(items, page, total_pages, "new"),
            )
            return

        # Danh sách theo loại
        if action == "list":
            loai = parts[2]; page = int(parts[3])
            await cb.answer("Đang tải...")
            try:
                data = await api_danh_sach(loai, page, 10)
                items = data.get("data", {}).get("items", [])
                pg = data.get("data", {}).get("params", {}).get("pagination", {})
                total_pages = pg.get("totalPages", 1)
            except Exception as e:
                await cb.answer(f"Lỗi: {e}", show_alert=True)
                return
            await cb.message.edit_text(
                f"📚 **{loai}** — Trang {page}",
                reply_markup=_list_kb(items, page, total_pages, f"list|{loai}"),
            )
            return

        # Tìm kiếm phân trang
        if action == "search":
            kw = parts[2]; page = int(parts[3])
            await cb.answer("Đang tải...")
            try:
                data = await api_tim_kiem(kw, page, 10)
                items = data.get("data", {}).get("items", [])
                pg = data.get("data", {}).get("params", {}).get("pagination", {})
                total_pages = pg.get("totalPages", 1)
            except Exception as e:
                await cb.answer(f"Lỗi: {e}", show_alert=True)
                return
            await cb.message.edit_text(
                f"🔍 **{kw}** — Trang {page}",
                reply_markup=_list_kb(items, page, total_pages, f"search|{kw}"),
            )
            return

        # Chi tiết phim
        if action == "film":
            slug = parts[2]
            await cb.answer("Đang tải phim...")
            try:
                data = await api_chi_tiet(slug)
                movie = data.get("movie", {})
                episodes = data.get("episodes", [])
            except Exception as e:
                await cb.answer(f"Lỗi: {e}", show_alert=True)
                return
            _film_cache[cb.message.id] = {
                "slug": slug, "movie": movie, "episodes": episodes,
                "server_idx": 0, "mode": "vplay",
            }
            info = _fmt_film_info(movie)
            await cb.message.edit_text(
                info, reply_markup=_film_kb(slug, episodes, 0, "vplay", 1),
                disable_web_page_preview=False,
            )
            return

        # Đổi server
        if action == "sv":
            slug = parts[2]; si = int(parts[3])
            c = _film_cache.get(cb.message.id)
            if not c:
                await cb.answer("Hết hạn, mở lại phim.", show_alert=True); return
            c["server_idx"] = si
            await cb.answer(f"Server {si+1}")
            await cb.message.edit_reply_markup(
                _film_kb(slug, c["episodes"], si, c["mode"], 1))
            return

        # Đổi chế độ phát
        if action == "mode":
            slug = parts[2]; mode = parts[3]
            c = _film_cache.get(cb.message.id)
            if not c:
                await cb.answer("Hết hạn, mở lại phim.", show_alert=True); return
            c["mode"] = mode
            await cb.answer(f"Chế độ: {mode}")
            await cb.message.edit_reply_markup(
                _film_kb(slug, c["episodes"], c["server_idx"], mode, 1))
            return

        # Phân trang tập
        if action == "eppage":
            slug = parts[2]; si = int(parts[3]); page = int(parts[4])
            c = _film_cache.get(cb.message.id)
            if not c:
                await cb.answer("Hết hạn.", show_alert=True); return
            await cb.answer()
            await cb.message.edit_reply_markup(
                _film_kb(slug, c["episodes"], si, c["mode"], page))
            return

        # Chọn tập → phát
        if action == "ep":
            slug = parts[2]; si = int(parts[3]); ep_idx = int(parts[4])
            c = _film_cache.get(cb.message.id)
            if not c:
                await cb.answer("Hết hạn, mở lại phim.", show_alert=True); return
            episodes = c["episodes"]
            try:
                ep = episodes[si]["server_data"][ep_idx]
                m3u8 = ep.get("link_m3u8") or ep.get("link_embed")
                ep_name = ep.get("name", "?")
            except Exception:
                await cb.answer("Không lấy được tập này.", show_alert=True); return
            if not m3u8:
                await cb.answer("Tập này chưa có link.", show_alert=True); return
            mode = c["mode"]
            movie_name = c["movie"].get("name", "Phim")
            title = f"{movie_name} - {ep_name}"
            await cb.answer(f"▶️ {ep_name} ({mode})")
            await cb.message.delete()
            # Gọi callback của bot chính để phát
            await on_play(client, cb.message.chat.id, title, m3u8, mode)
            return
