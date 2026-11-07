#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UZSARAFILM — Telegram kino boti (Kino / Serial kino / Anime / Drama)

Ishga tushirish:
    pip install aiogram aiosqlite
    export BOT_TOKEN="123456:ABC..."
    export OWNER_IDS="123456789"        # bir nechta bo'lsa vergul bilan: 111,222
    python bot.py

Imkoniyatlar:
  * /start -> majburiy obuna (kanallar) -> obunani tekshirish -> tur tanlash
  * Tur: Kino, Serial kino, Anime, Drama -> kod yoki nom bilan qidirish
  * Serial/Anime/Drama: poster + nom + qismlar soni + 10 talik qism tugmalari
    (Oldingi / Keyingisi sahifalash)
  * Admin panel: kontent/qism qo'shish-o'chirish, kanallar, adminlar,
    statistika, xabar yuborish (broadcast)
"""

import asyncio
import html
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ======================================================================
#  SOZLAMALAR
# ======================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9144381802:AAEYy_i4d1upgwvMj3m4IFvT35nbuYUWItU")
# Bosh adminlar (ular boshqa adminlarni qo'sha/o'chira oladi)
OWNER_IDS = {
    int(x)
    for x in os.getenv("OWNER_IDS", "8165064673").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
DB_PATH = os.getenv("DB_PATH", "uzsarafilm.db")
BRAND = "UZSARAFILM"
PER_PAGE = 10            # qism tugmalari soni (bir sahifada)
LIST_PER_PAGE = 10       # ro'yxatdagi kontentlar soni
PROTECT_CONTENT = False  # True bo'lsa videoni forward/saqlash taqiqlanadi
SUB_CACHE_SECONDS = 60   # obuna tekshiruvi keshi

CATS = {
    "kino": ("🎬", "Kino"),
    "serial": ("📺", "Serial kino"),
    "anime": ("🌸", "Anime"),
    "drama": ("🎭", "Drama"),
}

TZ = timezone(timedelta(hours=5))  # Toshkent vaqti
BOT_USERNAME = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(BRAND)

esc = html.escape


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ======================================================================
#  MA'LUMOTLAR BAZASI
# ======================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    full_name TEXT,
    username TEXT,
    joined_at TEXT,
    last_active TEXT
);
CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'tg',
    chat_id TEXT,
    title TEXT,
    link TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contents(
    code INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    poster TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS episodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_code INTEGER NOT NULL,
    ep_number INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT 'video',
    views INTEGER NOT NULL DEFAULT 0,
    UNIQUE(content_code, ep_number),
    FOREIGN KEY(content_code) REFERENCES contents(code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contents_cat ON contents(category);
CREATE INDEX IF NOT EXISTS idx_episodes_code ON episodes(content_code);
"""

db: aiosqlite.Connection


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(SCHEMA)
    await db.commit()


async def fetchone(sql: str, params: tuple = ()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, params: tuple = ()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def execute(sql: str, params: tuple = ()):
    cur = await db.execute(sql, params)
    await db.commit()
    return cur


async def upsert_user(u) -> None:
    ts = now_str()
    await db.execute(
        """INSERT INTO users(user_id, full_name, username, joined_at, last_active)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               full_name=excluded.full_name,
               username=excluded.username,
               last_active=excluded.last_active""",
        (u.id, u.full_name or "", u.username or "", ts, ts),
    )
    await db.commit()


async def is_admin(uid: int) -> bool:
    if uid in OWNER_IDS:
        return True
    return await fetchone("SELECT 1 FROM admins WHERE user_id=?", (uid,)) is not None


def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS


async def get_content(code: int, category: Optional[str] = None):
    if category:
        return await fetchone(
            "SELECT * FROM contents WHERE code=? AND category=?", (code, category)
        )
    return await fetchone("SELECT * FROM contents WHERE code=?", (code,))


async def get_ep_numbers(code: int) -> list:
    rows = await fetchall(
        "SELECT ep_number FROM episodes WHERE content_code=? ORDER BY ep_number",
        (code,),
    )
    return [r["ep_number"] for r in rows]


# ======================================================================
#  YORDAMCHI FUNKSIYALAR (tugmalar)
# ======================================================================
def B(text: str, data: Optional[str] = None, url: Optional[str] = None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def M(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chunk(items: list, n: int) -> list:
    return [items[i : i + n] for i in range(0, len(items), n)]


def menu_kb(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [B(f"{CATS['kino'][0]} {CATS['kino'][1]}", "cat:kino"),
         B(f"{CATS['serial'][0]} {CATS['serial'][1]}", "cat:serial")],
        [B(f"{CATS['anime'][0]} {CATS['anime'][1]}", "cat:anime"),
         B(f"{CATS['drama'][0]} {CATS['drama'][1]}", "cat:drama")],
    ]
    if admin:
        rows.append([B("⚙️ Admin panel", "adm:home")])
    return M(rows)


def episodes_kb(code: int, nums: list, page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(nums) / PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    part = nums[page * PER_PAGE : (page + 1) * PER_PAGE]
    rows = chunk([B(str(n), f"ep:{code}:{n}") for n in part], 5)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️ Oldingi", f"pg:{code}:{page - 1}"))
        nav.append(B(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(B("Keyingisi ➡️", f"pg:{code}:{page + 1}"))
        rows.append(nav)
    return M(rows)


def build_caption(c, ep_count: int) -> str:
    emoji, cname = CATS[c["category"]]
    lines = [
        f"{emoji} <b>{esc(c['title'])}</b>",
        "",
        f"📂 Turi: <b>{cname}</b>",
        f"🔑 Kod: <code>{c['code']}</code>",
        f"🎞 Qismlar soni: <b>{ep_count}</b>",
    ]
    if c["description"]:
        lines += ["", esc(c["description"])]
    footer = f"@{BOT_USERNAME}" if BOT_USERNAME else BRAND
    lines += ["", f"🤖 {footer}"]
    return "\n".join(lines)


async def edit_or_send(call: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        await call.message.answer(text, reply_markup=kb)


async def send_file(bot: Bot, chat_id: int, file_type: str, file_id: str,
                    caption: str, kb: Optional[InlineKeyboardMarkup] = None):
    if file_type == "document":
        return await bot.send_document(
            chat_id, file_id, caption=caption, reply_markup=kb,
            protect_content=PROTECT_CONTENT,
        )
    return await bot.send_video(
        chat_id, file_id, caption=caption, reply_markup=kb,
        protect_content=PROTECT_CONTENT, supports_streaming=True,
    )


# ======================================================================
#  OBUNA TEKSHIRUVI
# ======================================================================
_sub_cache: dict = {}


def _chat_ref(chat_id: str):
    s = str(chat_id)
    return int(s) if s.lstrip("-").isdigit() else s


async def get_unsubscribed(bot: Bot, user_id: int) -> list:
    rows = await fetchall("SELECT * FROM channels WHERE kind='tg' ORDER BY id")
    missing = []
    for ch in rows:
        try:
            m = await bot.get_chat_member(_chat_ref(ch["chat_id"]), user_id)
            ok = m.status in (
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER,
            ) or (
                m.status == ChatMemberStatus.RESTRICTED
                and getattr(m, "is_member", False)
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            # Bot kanalda admin emas / kanal topilmadi: foydalanuvchini bloklamaymiz
            log.warning("Kanalni tekshirib bo'lmadi (%s): %s", ch["chat_id"], e)
            ok = True
        if not ok:
            missing.append(ch)
    return missing


def subscribe_kb(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(channels, 1):
        rows.append([B(f"📢 {ch['title'] or 'Kanal'}", url=ch["link"])])
    rows.append([B("✅ Tekshirish", "check_sub")])
    return M(rows)


SUB_TEXT = (
    "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, "
    "so'ng <b>✅ Tekshirish</b> tugmasini bosing:"
)


async def ensure_subscribed(event: Union[Message, CallbackQuery], bot: Bot) -> bool:
    user = event.from_user
    if await is_admin(user.id):
        return True
    if _sub_cache.get(user.id, 0) > time.time():
        return True
    missing = await get_unsubscribed(bot, user.id)
    if not missing:
        _sub_cache[user.id] = time.time() + SUB_CACHE_SECONDS
        return True
    url_channels = await fetchall("SELECT * FROM channels WHERE kind='url' ORDER BY id")
    kb = subscribe_kb(list(missing) + list(url_channels))
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(SUB_TEXT, reply_markup=kb)
    else:
        await event.answer(SUB_TEXT, reply_markup=kb)
    return False


# ======================================================================
#  KONTENT YUBORISH
# ======================================================================
async def send_content(bot: Bot, chat_id: int, c, page: int = 0, count_view: bool = True):
    code = c["code"]
    nums = await get_ep_numbers(code)
    if count_view:
        await execute("UPDATE contents SET views=views+1 WHERE code=?", (code,))
    emoji, _ = CATS[c["category"]]

    # Oddiy kino (bitta video) — to'g'ridan-to'g'ri yuboramiz
    if c["category"] == "kino" and len(nums) == 1:
        ep = await fetchone(
            "SELECT * FROM episodes WHERE content_code=? AND ep_number=?",
            (code, nums[0]),
        )
        footer = f"@{BOT_USERNAME}" if BOT_USERNAME else BRAND
        caption = (
            f"{emoji} <b>{esc(c['title'])}</b>\n"
            f"🔑 Kod: <code>{code}</code>\n"
        )
        if c["description"]:
            caption += f"\n{esc(c['description'])}\n"
        caption += f"\n🤖 {footer}"
        await send_file(bot, chat_id, ep["file_type"], ep["file_id"], caption)
        await execute("UPDATE episodes SET views=views+1 WHERE id=?", (ep["id"],))
        return

    caption = build_caption(c, len(nums))
    if not nums:
        caption += "\n\n⏳ Qismlar tez orada qo'shiladi."
        await bot.send_photo(chat_id, c["poster"], caption=caption,
                             protect_content=PROTECT_CONTENT)
        return
    caption += "\n\n👇 Qismni tanlang:"
    await bot.send_photo(
        chat_id, c["poster"], caption=caption,
        reply_markup=episodes_kb(code, nums, page),
        protect_content=PROTECT_CONTENT,
    )


# ======================================================================
#  HOLATLAR (FSM)
# ======================================================================
class UserFlow(StatesGroup):
    waiting_code = State()


class AddContent(StatesGroup):
    code = State()
    poster = State()
    title = State()
    description = State()
    videos = State()


class AddEpisode(StatesGroup):
    code = State()


class DelContent(StatesGroup):
    code = State()


class DelEpisode(StatesGroup):
    code = State()
    number = State()


class ChannelAdd(StatesGroup):
    tg = State()
    tg_link = State()
    url_title = State()
    url_link = State()


class AdminAdd(StatesGroup):
    user_id = State()


class Broadcast(StatesGroup):
    message = State()
    confirm = State()


# ======================================================================
#  MIDDLEWARE & FILTR
# ======================================================================
class UserTrackMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                await upsert_user(user)
            except Exception as e:  # bazaga yozilmasa ham bot ishlayversin
                log.warning("upsert_user xatosi: %s", e)
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        return await is_admin(event.from_user.id)


admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())
user_router = Router(name="user")

NOT_CMD = ~F.text.startswith("/")


# ======================================================================
#  ADMIN: PANEL
# ======================================================================
def admin_panel_kb() -> InlineKeyboardMarkup:
    return M([
        [B("➕ Kontent qo'shish", "adm:add"), B("🎞 Qism qo'shish", "adm:addep")],
        [B("🗑 Kontent o'chirish", "adm:delc"), B("❌ Qism o'chirish", "adm:dele")],
        [B("📚 Kontentlar", "adm:ls"), B("📢 Kanallar", "adm:ch")],
        [B("👮 Adminlar", "adm:admins"), B("📊 Statistika", "adm:stats")],
        [B("📨 Xabar yuborish", "adm:bc")],
        [B("🏠 Bosh menyu", "menu")],
    ])


PANEL_TEXT = f"⚙️ <b>{BRAND} — Admin panel</b>\n\nKerakli bo'limni tanlang:"
BACK_ADMIN = M([[B("🔙 Admin panel", "adm:home")]])


@admin_router.message(Command("admin"))
async def adm_cmd(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(PANEL_TEXT, reply_markup=admin_panel_kb())


@admin_router.message(Command("cancel"))
async def adm_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Bekor qilindi.", reply_markup=admin_panel_kb())


@admin_router.callback_query(F.data == "adm:home")
async def adm_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await edit_or_send(call, PANEL_TEXT, admin_panel_kb())


# ---------------- Kontent qo'shish ----------------
@admin_router.callback_query(F.data == "adm:add")
async def adm_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    rows = chunk([B(f"{e} {n}", f"adm:addc:{k}") for k, (e, n) in CATS.items()], 2)
    rows.append([B("🔙 Admin panel", "adm:home")])
    await edit_or_send(call, "➕ <b>Qaysi turdagi kontent qo'shamiz?</b>", M(rows))


@admin_router.callback_query(F.data.startswith("adm:addc:"))
async def adm_add_cat(call: CallbackQuery, state: FSMContext):
    cat = call.data.split(":")[2]
    if cat not in CATS:
        return await call.answer("Noto'g'ri tur", show_alert=True)
    await state.clear()
    await state.update_data(category=cat)
    await state.set_state(AddContent.code)
    await call.answer()
    await edit_or_send(
        call,
        f"{CATS[cat][0]} <b>{CATS[cat][1]}</b> qo'shilmoqda.\n\n"
        "🔑 Kontent <b>kodini</b> yuboring (faqat raqam) yoki avto-kodni tanlang:",
        M([[B("🎲 Avto kod", "adm:autocode")], [B("🔙 Bekor qilish", "adm:home")]]),
    )


async def _ask_poster(target: Union[Message, CallbackQuery], state: FSMContext, code: int):
    await state.update_data(code=code)
    await state.set_state(AddContent.poster)
    text = f"✅ Kod: <code>{code}</code>\n\n🖼 Endi <b>poster rasmini</b> yuboring (rasm sifatida):"
    if isinstance(target, CallbackQuery):
        await target.answer()
        await edit_or_send(target, text, BACK_ADMIN)
    else:
        await target.answer(text, reply_markup=BACK_ADMIN)


@admin_router.callback_query(AddContent.code, F.data == "adm:autocode")
async def adm_autocode(call: CallbackQuery, state: FSMContext):
    row = await fetchone("SELECT COALESCE(MAX(code),0)+1 AS n FROM contents")
    await _ask_poster(call, state, row["n"])


@admin_router.message(AddContent.code, F.text, NOT_CMD)
async def adm_code(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not t.isdigit() or int(t) <= 0:
        return await msg.answer("❗ Kod faqat musbat raqam bo'lishi kerak. Qayta yuboring:")
    if await get_content(int(t)):
        return await msg.answer("❗ Bu kod band. Boshqa kod yuboring yoki 🎲 Avto kod ishlating.")
    await _ask_poster(msg, state, int(t))


@admin_router.message(AddContent.poster, F.photo)
async def adm_poster(msg: Message, state: FSMContext):
    await state.update_data(poster=msg.photo[-1].file_id)
    await state.set_state(AddContent.title)
    await msg.answer("✏️ Kontent <b>nomini</b> yuboring:", reply_markup=BACK_ADMIN)


@admin_router.message(AddContent.poster)
async def adm_poster_bad(msg: Message):
    if msg.text and msg.text.startswith("/"):
        return
    await msg.answer("❗ Iltimos, poster uchun <b>rasm</b> yuboring.")


@admin_router.message(AddContent.title, F.text, NOT_CMD)
async def adm_title(msg: Message, state: FSMContext):
    title = msg.text.strip()[:200]
    await state.update_data(title=title)
    await state.set_state(AddContent.description)
    await msg.answer(
        "📝 Qisqa <b>tavsif</b> yuboring (janr, yil, til va h.k.) yoki o'tkazib yuboring:",
        reply_markup=M([[B("⏭ O'tkazib yuborish", "adm:skipdesc")],
                        [B("🔙 Bekor qilish", "adm:home")]]),
    )


async def _finalize_content(target: Union[Message, CallbackQuery], state: FSMContext,
                            description: Optional[str]):
    data = await state.get_data()
    code, cat = data["code"], data["category"]
    await execute(
        "INSERT INTO contents(code, category, title, description, poster, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (code, cat, data["title"], description, data["poster"], now_str()),
    )
    await state.set_state(AddContent.videos)
    await state.update_data(added=0)
    if cat == "kino":
        text = (f"✅ <b>{esc(data['title'])}</b> yaratildi (kod: <code>{code}</code>).\n\n"
                "🎬 Endi <b>kino videosini</b> yuboring:")
        kb = BACK_ADMIN
    else:
        text = (f"✅ <b>{esc(data['title'])}</b> yaratildi (kod: <code>{code}</code>).\n\n"
                "🎞 Endi <b>qismlarni ketma-ket</b> yuboring (1-qism, 2-qism, ...). "
                "Tugagach <b>✅ Tayyor</b> tugmasini bosing:")
        kb = M([[B("✅ Tayyor", "adm:done")]])
    if isinstance(target, CallbackQuery):
        await target.answer()
        await edit_or_send(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


@admin_router.callback_query(AddContent.description, F.data == "adm:skipdesc")
async def adm_skip_desc(call: CallbackQuery, state: FSMContext):
    await _finalize_content(call, state, None)


@admin_router.message(AddContent.description, F.text, NOT_CMD)
async def adm_desc(msg: Message, state: FSMContext):
    await _finalize_content(msg, state, msg.text.strip()[:600])


@admin_router.message(AddContent.videos, F.video | F.document)
async def adm_video(msg: Message, state: FSMContext):
    data = await state.get_data()
    code, cat = data["code"], data.get("category", "serial")
    row = await fetchone(
        "SELECT COALESCE(MAX(ep_number),0)+1 AS n FROM episodes WHERE content_code=?",
        (code,),
    )
    n = row["n"]
    if msg.video:
        file_id, ftype = msg.video.file_id, "video"
    else:
        file_id, ftype = msg.document.file_id, "document"
    await execute(
        "INSERT INTO episodes(content_code, ep_number, file_id, file_type) VALUES(?,?,?,?)",
        (code, n, file_id, ftype),
    )
    added = data.get("added", 0) + 1
    await state.update_data(added=added)
    if cat == "kino" and n == 1:
        await state.clear()
        return await msg.answer(
            f"🎉 Kino qo'shildi!\n🔑 Kod: <code>{code}</code>",
            reply_markup=admin_panel_kb(),
        )
    await msg.answer(
        f"✅ <b>{n}-qism</b> qo'shildi (shu safar: {added} ta).\n"
        "Keyingi qismni yuboring yoki tugating:",
        reply_markup=M([[B("✅ Tayyor", "adm:done")]]),
    )


@admin_router.message(AddContent.videos)
async def adm_video_bad(msg: Message):
    if msg.text and msg.text.startswith("/"):
        return
    await msg.answer("❗ Iltimos, <b>video</b> (yoki fayl) yuboring.")


@admin_router.callback_query(AddContent.videos, F.data == "adm:done")
async def adm_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await edit_or_send(
        call,
        f"🎉 Tayyor! Kod: <code>{data.get('code')}</code>\n"
        f"Qo'shilgan qismlar: <b>{data.get('added', 0)}</b>",
        admin_panel_kb(),
    )


# ---------------- Mavjud kontentga qism qo'shish ----------------
@admin_router.callback_query(F.data == "adm:addep")
async def adm_addep(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddEpisode.code)
    await call.answer()
    await edit_or_send(call, "🎞 Qism qo'shiladigan kontent <b>kodini</b> yuboring:", BACK_ADMIN)


@admin_router.message(AddEpisode.code, F.text, NOT_CMD)
async def adm_addep_code(msg: Message, state: FSMContext):
    t = msg.text.strip()
    c = await get_content(int(t)) if t.isdigit() else None
    if not c:
        return await msg.answer("❗ Bunday kodli kontent topilmadi. Qayta yuboring:")
    nums = await get_ep_numbers(c["code"])
    await state.clear()
    await state.set_state(AddContent.videos)
    await state.update_data(code=c["code"], category=c["category"], added=0)
    await msg.answer(
        f"📌 <b>{esc(c['title'])}</b> — hozir {len(nums)} ta qism bor.\n\n"
        f"Yangi qismlarni ketma-ket yuboring ({(nums[-1] + 1) if nums else 1}-qismdan boshlanadi):",
        reply_markup=M([[B("✅ Tayyor", "adm:done")]]),
    )


# ---------------- Kontent o'chirish ----------------
@admin_router.callback_query(F.data == "adm:delc")
async def adm_delc(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DelContent.code)
    await call.answer()
    await edit_or_send(call, "🗑 O'chiriladigan kontent <b>kodini</b> yuboring:", BACK_ADMIN)


@admin_router.message(DelContent.code, F.text, NOT_CMD)
async def adm_delc_code(msg: Message, state: FSMContext):
    t = msg.text.strip()
    c = await get_content(int(t)) if t.isdigit() else None
    if not c:
        return await msg.answer("❗ Bunday kodli kontent topilmadi. Qayta yuboring:")
    nums = await get_ep_numbers(c["code"])
    await state.clear()
    await msg.answer(
        f"⚠️ <b>{esc(c['title'])}</b> ({CATS[c['category']][1]}, {len(nums)} qism) "
        "butunlay o'chirilsinmi?",
        reply_markup=M([[B("✅ Ha, o'chirish", f"adm:delcy:{c['code']}"),
                         B("❌ Yo'q", "adm:home")]]),
    )


@admin_router.callback_query(F.data.startswith("adm:delcy:"))
async def adm_delc_yes(call: CallbackQuery):
    code = int(call.data.split(":")[2])
    await execute("DELETE FROM episodes WHERE content_code=?", (code,))
    await execute("DELETE FROM contents WHERE code=?", (code,))
    await call.answer("O'chirildi ✅")
    await edit_or_send(call, f"🗑 Kontent <code>{code}</code> o'chirildi.", admin_panel_kb())


# ---------------- Qism o'chirish ----------------
@admin_router.callback_query(F.data == "adm:dele")
async def adm_dele(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DelEpisode.code)
    await call.answer()
    await edit_or_send(call, "❌ Qismi o'chiriladigan kontent <b>kodini</b> yuboring:", BACK_ADMIN)


@admin_router.message(DelEpisode.code, F.text, NOT_CMD)
async def adm_dele_code(msg: Message, state: FSMContext):
    t = msg.text.strip()
    c = await get_content(int(t)) if t.isdigit() else None
    if not c:
        return await msg.answer("❗ Bunday kodli kontent topilmadi. Qayta yuboring:")
    nums = await get_ep_numbers(c["code"])
    if not nums:
        await state.clear()
        return await msg.answer("Bu kontentda qism yo'q.", reply_markup=admin_panel_kb())
    await state.update_data(code=c["code"])
    await state.set_state(DelEpisode.number)
    await msg.answer(
        f"📌 <b>{esc(c['title'])}</b>\nMavjud qismlar: {', '.join(map(str, nums))}\n\n"
        "O'chiriladigan <b>qism raqamini</b> yuboring:",
        reply_markup=BACK_ADMIN,
    )


@admin_router.message(DelEpisode.number, F.text, NOT_CMD)
async def adm_dele_num(msg: Message, state: FSMContext):
    t = msg.text.strip()
    data = await state.get_data()
    code = data["code"]
    if not t.isdigit():
        return await msg.answer("❗ Raqam yuboring:")
    cur = await execute(
        "DELETE FROM episodes WHERE content_code=? AND ep_number=?", (code, int(t))
    )
    await state.clear()
    if cur.rowcount:
        await msg.answer(f"✅ {t}-qism o'chirildi.", reply_markup=admin_panel_kb())
    else:
        await msg.answer("❗ Bunday qism topilmadi.", reply_markup=admin_panel_kb())


# ---------------- Kontentlar ro'yxati ----------------
@admin_router.callback_query(F.data == "adm:ls")
async def adm_ls(call: CallbackQuery):
    await call.answer()
    rows = chunk([B(f"{e} {n}", f"adm:lc:{k}:0") for k, (e, n) in CATS.items()], 2)
    rows.append([B("🔙 Admin panel", "adm:home")])
    await edit_or_send(call, "📚 <b>Qaysi tur ro'yxatini ko'ramiz?</b>", M(rows))


@admin_router.callback_query(F.data.startswith("adm:lc:"))
async def adm_list(call: CallbackQuery):
    _, _, cat, page = call.data.split(":")
    page = int(page)
    per = 15
    total = (await fetchone("SELECT COUNT(*) AS n FROM contents WHERE category=?", (cat,)))["n"]
    pages = max(1, math.ceil(total / per))
    page = max(0, min(page, pages - 1))
    rows = await fetchall(
        """SELECT c.code, c.title,
                  (SELECT COUNT(*) FROM episodes e WHERE e.content_code=c.code) AS n
           FROM contents c WHERE c.category=? ORDER BY c.code DESC LIMIT ? OFFSET ?""",
        (cat, per, page * per),
    )
    lines = [f"{CATS[cat][0]} <b>{CATS[cat][1]}</b> — jami: {total}\n"]
    if not rows:
        lines.append("Hozircha bo'sh.")
    for r in rows:
        lines.append(f"<code>{r['code']}</code> — {esc(r['title'])} ({r['n']} qism)")
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"adm:lc:{cat}:{page - 1}"))
    nav.append(B(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(B("➡️", f"adm:lc:{cat}:{page + 1}"))
    await call.answer()
    await edit_or_send(call, "\n".join(lines),
                       M([nav, [B("🔙 Orqaga", "adm:ls")]]))


# ---------------- Kanallar ----------------
async def _channels_view(call: CallbackQuery):
    rows = await fetchall("SELECT * FROM channels ORDER BY id")
    if rows:
        lines = ["📢 <b>Majburiy obuna kanallari:</b>\n"]
        kb = []
        for r in rows:
            tag = "✅ tekshiriladi" if r["kind"] == "tg" else "🔗 havola"
            lines.append(f"• {esc(r['title'] or '-')} — {tag}")
            kb.append([B(f"🗑 {r['title'] or r['link']}", f"adm:chdel:{r['id']}")])
    else:
        lines = ["📢 Hozircha majburiy obuna kanallari yo'q."]
        kb = []
    kb.append([B("➕ Kanal (tekshiriladi)", "adm:chadd:tg")])
    kb.append([B("🔗 Havola (tekshirilmaydi)", "adm:chadd:url")])
    kb.append([B("🔙 Admin panel", "adm:home")])
    await edit_or_send(call, "\n".join(lines), M(kb))


@admin_router.callback_query(F.data == "adm:ch")
async def adm_channels(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _channels_view(call)


@admin_router.callback_query(F.data.startswith("adm:chdel:"))
async def adm_channel_del(call: CallbackQuery):
    cid = int(call.data.split(":")[2])
    await execute("DELETE FROM channels WHERE id=?", (cid,))
    _sub_cache.clear()
    await call.answer("O'chirildi ✅")
    await _channels_view(call)


@admin_router.callback_query(F.data == "adm:chadd:tg")
async def adm_chadd_tg(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ChannelAdd.tg)
    await call.answer()
    me = await call.bot.get_me()
    await edit_or_send(
        call,
        "📢 <b>Kanal qo'shish</b>\n\n"
        f"1) Avval @{me.username} ni kanalga <b>admin</b> qiling.\n"
        "2) Keyin kanaldan <b>biror postni forward</b> qiling yoki kanal "
        "<code>@username</code> / ID (<code>-100...</code>) ini yuboring.",
        BACK_ADMIN,
    )


async def _save_channel(kind: str, chat_id: Optional[str], title: str, link: str):
    await execute(
        "INSERT INTO channels(kind, chat_id, title, link) VALUES(?,?,?,?)",
        (kind, chat_id, title, link),
    )
    _sub_cache.clear()


@admin_router.message(ChannelAdd.tg, NOT_CMD | ~F.text)
async def adm_ch_tg_input(msg: Message, state: FSMContext, bot: Bot):
    ref = None
    fo = msg.forward_origin
    if fo is not None and getattr(fo, "chat", None) is not None:
        ref = fo.chat.id
    elif msg.text:
        t = msg.text.strip()
        if t.startswith(("https://t.me/", "http://t.me/")):
            tail = t.rstrip("/").split("/")[-1]
            if tail.startswith("+") or "joinchat" in t:
                return await msg.answer(
                    "❗ Maxfiy havoladan kanalni aniqlab bo'lmaydi. "
                    "Kanaldan biror postni <b>forward</b> qiling yoki ID yuboring."
                )
            t = "@" + tail
        if t.startswith("@"):
            ref = t
        elif t.lstrip("-").isdigit():
            ref = int(t)
    if ref is None:
        return await msg.answer("❗ Kanalni aniqlay olmadim. Post forward qiling yoki @username/ID yuboring.")
    try:
        chat = await bot.get_chat(ref)
    except Exception:
        return await msg.answer("❗ Kanal topilmadi. Bot kanalga qo'shilganini tekshiring.")
    try:
        me = await bot.get_me()
        m = await bot.get_chat_member(chat.id, me.id)
        if m.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            raise ValueError
    except Exception:
        return await msg.answer("❗ Bot bu kanalda <b>admin</b> emas. Avval botni admin qiling.")
    if await fetchone("SELECT 1 FROM channels WHERE chat_id=?", (str(chat.id),)):
        await state.clear()
        return await msg.answer("ℹ️ Bu kanal allaqachon qo'shilgan.", reply_markup=admin_panel_kb())
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            inv = await bot.create_chat_invite_link(chat.id)
            link = inv.invite_link
        except Exception:
            link = None
    if link:
        await _save_channel("tg", str(chat.id), chat.title or "Kanal", link)
        await state.clear()
        return await msg.answer(
            f"✅ <b>{esc(chat.title or 'Kanal')}</b> qo'shildi.", reply_markup=admin_panel_kb()
        )
    await state.update_data(chat_id=str(chat.id), title=chat.title or "Kanal")
    await state.set_state(ChannelAdd.tg_link)
    await msg.answer("🔗 Kanal uchun taklif havolasini yuboring (https://t.me/+...):")


@admin_router.message(ChannelAdd.tg_link, F.text, NOT_CMD)
async def adm_ch_tg_link(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link.startswith("http"):
        return await msg.answer("❗ Havola https:// bilan boshlanishi kerak.")
    data = await state.get_data()
    await _save_channel("tg", data["chat_id"], data["title"], link)
    await state.clear()
    await msg.answer("✅ Kanal qo'shildi.", reply_markup=admin_panel_kb())


@admin_router.callback_query(F.data == "adm:chadd:url")
async def adm_chadd_url(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ChannelAdd.url_title)
    await call.answer()
    await edit_or_send(call, "🔗 Tugma uchun <b>nom</b> yuboring (masalan: Instagram):", BACK_ADMIN)


@admin_router.message(ChannelAdd.url_title, F.text, NOT_CMD)
async def adm_ch_url_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip()[:60])
    await state.set_state(ChannelAdd.url_link)
    await msg.answer("🔗 Endi <b>havolani</b> yuboring (https://...):")


@admin_router.message(ChannelAdd.url_link, F.text, NOT_CMD)
async def adm_ch_url_link(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link.startswith("http"):
        return await msg.answer("❗ Havola https:// bilan boshlanishi kerak.")
    data = await state.get_data()
    await _save_channel("url", None, data["title"], link)
    await state.clear()
    await msg.answer("✅ Havola qo'shildi.", reply_markup=admin_panel_kb())


# ---------------- Adminlar ----------------
async def _admins_view(call: CallbackQuery):
    rows = await fetchall("SELECT * FROM admins ORDER BY added_at")
    lines = ["👮 <b>Adminlar</b>\n"]
    for oid in sorted(OWNER_IDS):
        lines.append(f"👑 <code>{oid}</code> — bosh admin")
    kb = []
    for r in rows:
        u = await fetchone("SELECT full_name FROM users WHERE user_id=?", (r["user_id"],))
        name = esc(u["full_name"]) if u and u["full_name"] else "—"
        lines.append(f"👮 <code>{r['user_id']}</code> — {name}")
        if is_owner(call.from_user.id):
            kb.append([B(f"🗑 {r['user_id']}", f"adm:addel:{r['user_id']}")])
    if is_owner(call.from_user.id):
        kb.append([B("➕ Admin qo'shish", "adm:addadmin")])
    kb.append([B("🔙 Admin panel", "adm:home")])
    await edit_or_send(call, "\n".join(lines), M(kb))


@admin_router.callback_query(F.data == "adm:admins")
async def adm_admins(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _admins_view(call)


@admin_router.callback_query(F.data == "adm:addadmin")
async def adm_addadmin(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("Faqat bosh admin qo'sha oladi.", show_alert=True)
    await state.clear()
    await state.set_state(AdminAdd.user_id)
    await call.answer()
    await edit_or_send(
        call,
        "👮 Yangi adminning <b>Telegram ID</b> raqamini yuboring yoki "
        "uning biror xabarini forward qiling.",
        BACK_ADMIN,
    )


@admin_router.message(AdminAdd.user_id, NOT_CMD | ~F.text)
async def adm_addadmin_id(msg: Message, state: FSMContext, bot: Bot):
    if not is_owner(msg.from_user.id):
        return
    uid = None
    fo = msg.forward_origin
    if fo is not None and getattr(fo, "sender_user", None) is not None:
        uid = fo.sender_user.id
    elif msg.text and msg.text.strip().isdigit():
        uid = int(msg.text.strip())
    if uid is None:
        return await msg.answer("❗ ID raqam yuboring yoki foydalanuvchi xabarini forward qiling.")
    if uid in OWNER_IDS or await fetchone("SELECT 1 FROM admins WHERE user_id=?", (uid,)):
        await state.clear()
        return await msg.answer("ℹ️ Bu foydalanuvchi allaqachon admin.", reply_markup=admin_panel_kb())
    await execute(
        "INSERT INTO admins(user_id, added_by, added_at) VALUES(?,?,?)",
        (uid, msg.from_user.id, now_str()),
    )
    await state.clear()
    await msg.answer(f"✅ <code>{uid}</code> admin qilindi.", reply_markup=admin_panel_kb())
    try:
        await bot.send_message(uid, "🎉 Siz botda <b>admin</b> etib tayinlandingiz. /admin")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("adm:addel:"))
async def adm_admin_del(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("Faqat bosh admin o'chira oladi.", show_alert=True)
    uid = int(call.data.split(":")[2])
    await execute("DELETE FROM admins WHERE user_id=?", (uid,))
    await call.answer("Admin o'chirildi ✅")
    await _admins_view(call)


# ---------------- Statistika ----------------
@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    day_ago = (datetime.now(TZ) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    total = (await fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    new_today = (await fetchone(
        "SELECT COUNT(*) AS n FROM users WHERE joined_at LIKE ?", (today + "%",)))["n"]
    active = (await fetchone(
        "SELECT COUNT(*) AS n FROM users WHERE last_active >= ?", (day_ago,)))["n"]
    cats = {r["category"]: r["n"] for r in await fetchall(
        "SELECT category, COUNT(*) AS n FROM contents GROUP BY category")}
    eps = (await fetchone("SELECT COUNT(*) AS n FROM episodes"))["n"]
    views = (await fetchone("SELECT COALESCE(SUM(views),0) AS n FROM contents"))["n"]
    chs = (await fetchone("SELECT COUNT(*) AS n FROM channels"))["n"]
    adms = (await fetchone("SELECT COUNT(*) AS n FROM admins"))["n"] + len(OWNER_IDS)
    top = await fetchall("SELECT code, title, views FROM contents ORDER BY views DESC LIMIT 5")
    lines = [
        f"📊 <b>{BRAND} statistikasi</b>\n",
        f"👥 Foydalanuvchilar: <b>{total}</b>",
        f"🆕 Bugun qo'shilgan: <b>{new_today}</b>",
        f"🔥 Oxirgi 24 soatda faol: <b>{active}</b>\n",
    ]
    for k, (e, n) in CATS.items():
        lines.append(f"{e} {n}: <b>{cats.get(k, 0)}</b>")
    lines += [
        f"\n🎞 Jami qismlar: <b>{eps}</b>",
        f"👁 Jami ko'rishlar: <b>{views}</b>",
        f"📢 Kanallar: <b>{chs}</b>  |  👮 Adminlar: <b>{adms}</b>",
    ]
    if top and top[0]["views"] > 0:
        lines.append("\n🏆 <b>Top 5:</b>")
        for t in top:
            lines.append(f"• {esc(t['title'])} (<code>{t['code']}</code>) — {t['views']} 👁")
    await call.answer()
    await edit_or_send(call, "\n".join(lines), BACK_ADMIN)


# ---------------- Xabar yuborish (broadcast) ----------------
_bg_tasks: set = set()


@admin_router.callback_query(F.data == "adm:bc")
async def adm_bc(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Broadcast.message)
    await call.answer()
    await edit_or_send(
        call,
        "📨 Barcha foydalanuvchilarga yuboriladigan <b>xabarni</b> yuboring "
        "(matn, rasm, video — istalgani):",
        BACK_ADMIN,
    )


@admin_router.message(Broadcast.message, NOT_CMD | ~F.text)
async def adm_bc_msg(msg: Message, state: FSMContext):
    await state.update_data(src_chat=msg.chat.id, src_msg=msg.message_id)
    await state.set_state(Broadcast.confirm)
    total = (await fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    await msg.answer(
        f"📨 Ushbu xabar <b>{total}</b> ta foydalanuvchiga yuborilsinmi?",
        reply_markup=M([[B("✅ Yuborish", "adm:bcy"), B("❌ Bekor qilish", "adm:home")]]),
    )


async def run_broadcast(bot: Bot, admin_id: int, src_chat: int, src_msg: int):
    rows = await fetchall("SELECT user_id FROM users")
    ok = fail = 0
    for r in rows:
        uid = r["user_id"]
        try:
            await bot.copy_message(uid, src_chat, src_msg)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
            except Exception:
                fail += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    try:
        await bot.send_message(
            admin_id,
            f"📨 <b>Xabar yuborish yakunlandi</b>\n\n✅ Yetkazildi: <b>{ok}</b>\n❌ Yetmadi: <b>{fail}</b>",
        )
    except Exception:
        pass


@admin_router.callback_query(Broadcast.confirm, F.data == "adm:bcy")
async def adm_bc_yes(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await call.answer("Yuborish boshlandi 🚀")
    await edit_or_send(call, "🚀 Xabar yuborilmoqda... Tugagach hisobot keladi.", admin_panel_kb())
    task = asyncio.create_task(
        run_broadcast(bot, call.from_user.id, data["src_chat"], data["src_msg"])
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ======================================================================
#  FOYDALANUVCHI QISMI
# ======================================================================
START_TEXT = (
    "👋 Assalomu alaykum, <b>{name}</b>!\n\n"
    f"🎬 <b>{BRAND}</b> botiga xush kelibsiz!\n"
    "Bu yerda eng sara kino, serial, anime va dramalarni tomosha qilishingiz mumkin.\n\n"
    "🍿 <b>Qaysi turdagi kinoni ko'rmoqchisiz?</b>"
)
MENU_TEXT = "🍿 <b>Qaysi turdagi kinoni ko'rmoqchisiz?</b>"


@user_router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    rows = await fetchall("SELECT * FROM channels ORDER BY id")
    if rows and not await is_admin(msg.from_user.id):
        if not await ensure_subscribed(msg, bot):
            return
    await msg.answer(
        START_TEXT.format(name=esc(msg.from_user.full_name)),
        reply_markup=menu_kb(await is_admin(msg.from_user.id)),
    )


@user_router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(MENU_TEXT, reply_markup=menu_kb(await is_admin(msg.from_user.id)))


@user_router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@user_router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    missing = await get_unsubscribed(bot, call.from_user.id)
    if missing:
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
        url_channels = await fetchall("SELECT * FROM channels WHERE kind='url' ORDER BY id")
        try:
            await call.message.edit_reply_markup(
                reply_markup=subscribe_kb(list(missing) + list(url_channels))
            )
        except TelegramBadRequest:
            pass
        return
    _sub_cache[call.from_user.id] = time.time() + SUB_CACHE_SECONDS
    await call.answer("✅ Obuna tasdiqlandi!")
    await edit_or_send(
        call,
        START_TEXT.format(name=esc(call.from_user.full_name)),
        menu_kb(await is_admin(call.from_user.id)),
    )


@user_router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    if not await ensure_subscribed(call, bot):
        return
    await call.answer()
    await edit_or_send(call, MENU_TEXT, menu_kb(await is_admin(call.from_user.id)))


@user_router.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    cat = call.data.split(":")[1]
    if cat not in CATS:
        return await call.answer()
    await state.clear()
    await state.set_state(UserFlow.waiting_code)
    await state.update_data(category=cat)
    emoji, name = CATS[cat]
    await call.answer()
    await edit_or_send(
        call,
        f"{emoji} <b>{name}</b> bo'limi\n\n"
        "🔑 Kontent <b>kodini</b> yuboring yoki <b>nomini</b> yozib qidiring.\n"
        "Yoki ro'yxatdan tanlang 👇",
        M([[B("📚 Barchasi (ro'yxat)", f"cl:{cat}:0")],
           [B("⬅️ Orqaga", "menu")]]),
    )


@user_router.callback_query(F.data.startswith("cl:"))
async def cb_cat_list(call: CallbackQuery, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    _, cat, page = call.data.split(":")
    page = int(page)
    total = (await fetchone("SELECT COUNT(*) AS n FROM contents WHERE category=?", (cat,)))["n"]
    pages = max(1, math.ceil(total / LIST_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = await fetchall(
        "SELECT code, title FROM contents WHERE category=? ORDER BY code DESC LIMIT ? OFFSET ?",
        (cat, LIST_PER_PAGE, page * LIST_PER_PAGE),
    )
    emoji, name = CATS[cat]
    kb = [[B(f"{r['title']} · #{r['code']}"[:60], f"open:{r['code']}")] for r in rows]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️ Oldingi", f"cl:{cat}:{page - 1}"))
        nav.append(B(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(B("Keyingisi ➡️", f"cl:{cat}:{page + 1}"))
        kb.append(nav)
    kb.append([B("⬅️ Orqaga", f"cat:{cat}")])
    text = f"{emoji} <b>{name}</b> — jami: {total}\n\n"
    text += "Birini tanlang 👇" if rows else "Hozircha bu bo'limda kontent yo'q."
    await call.answer()
    await edit_or_send(call, text, M(kb))


@user_router.callback_query(F.data.startswith("open:"))
async def cb_open(call: CallbackQuery, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    c = await get_content(int(call.data.split(":")[1]))
    if not c:
        return await call.answer("Topilmadi", show_alert=True)
    await call.answer()
    await send_content(bot, call.message.chat.id, c)


@user_router.callback_query(F.data.startswith("pg:"))
async def cb_page(call: CallbackQuery, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    _, code, page = call.data.split(":")
    nums = await get_ep_numbers(int(code))
    if not nums:
        return await call.answer("Qismlar topilmadi", show_alert=True)
    await call.answer()
    try:
        await call.message.edit_reply_markup(
            reply_markup=episodes_kb(int(code), nums, int(page))
        )
    except TelegramBadRequest:
        pass


@user_router.callback_query(F.data.startswith("lst:"))
async def cb_list_again(call: CallbackQuery, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    _, code, page = call.data.split(":")
    c = await get_content(int(code))
    if not c:
        return await call.answer("Topilmadi", show_alert=True)
    await call.answer()
    await send_content(bot, call.message.chat.id, c, page=int(page), count_view=False)


@user_router.callback_query(F.data.startswith("ep:"))
async def cb_episode(call: CallbackQuery, bot: Bot):
    if not await ensure_subscribed(call, bot):
        return
    _, code, n = call.data.split(":")
    code, n = int(code), int(n)
    c = await get_content(code)
    ep = await fetchone(
        "SELECT * FROM episodes WHERE content_code=? AND ep_number=?", (code, n)
    )
    if not c or not ep:
        return await call.answer("Qism topilmadi", show_alert=True)
    nums = await get_ep_numbers(code)
    idx = nums.index(n)
    nav = []
    if idx > 0:
        nav.append(B("⬅️ Oldingi qism", f"ep:{code}:{nums[idx - 1]}"))
    if idx < len(nums) - 1:
        nav.append(B("Keyingi qism ➡️", f"ep:{code}:{nums[idx + 1]}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([B("📋 Qismlar ro'yxati", f"lst:{code}:{idx // PER_PAGE}")])
    emoji, _ = CATS[c["category"]]
    footer = f"@{BOT_USERNAME}" if BOT_USERNAME else BRAND
    caption = (
        f"{emoji} <b>{esc(c['title'])}</b>\n"
        f"📺 <b>{n}-qism</b> ({idx + 1}/{len(nums)})\n"
        f"🔑 Kod: <code>{code}</code>\n\n🤖 {footer}"
    )
    await call.answer()
    await send_file(bot, call.message.chat.id, ep["file_type"], ep["file_id"],
                    caption, M(rows))
    await execute("UPDATE episodes SET views=views+1 WHERE id=?", (ep["id"],))


async def handle_query(msg: Message, bot: Bot, category: Optional[str]):
    """Kod yoki nom bo'yicha qidirish."""
    text = msg.text.strip()
    if text.isdigit():
        c = await get_content(int(text), category)
        if c:
            return await send_content(bot, msg.chat.id, c)
        where = f"«{CATS[category][1]}» bo'limida" if category else "bazada"
        return await msg.answer(f"😕 <code>{esc(text)}</code> kodli kontent {where} topilmadi.\n"
                                "Kodni tekshirib qayta yuboring.")
    if len(text) < 2:
        return await msg.answer("🔎 Qidirish uchun kamida 2 ta belgi yozing.")
    like = f"%{text}%"
    if category:
        rows = await fetchall(
            "SELECT code, title, category FROM contents WHERE title LIKE ? AND category=? "
            "ORDER BY views DESC LIMIT 10", (like, category))
    else:
        rows = await fetchall(
            "SELECT code, title, category FROM contents WHERE title LIKE ? "
            "ORDER BY views DESC LIMIT 10", (like,))
    if not rows:
        return await msg.answer("😕 Hech narsa topilmadi. Boshqacha yozib ko'ring yoki kod yuboring.")
    if len(rows) == 1:
        c = await get_content(rows[0]["code"])
        return await send_content(bot, msg.chat.id, c)
    kb = [[B(f"{CATS[r['category']][0]} {r['title']} · #{r['code']}"[:60], f"open:{r['code']}")]
          for r in rows]
    await msg.answer("🔎 <b>Qidiruv natijalari:</b>", reply_markup=M(kb))


@user_router.message(UserFlow.waiting_code, F.text, NOT_CMD)
async def msg_code_in_state(msg: Message, state: FSMContext, bot: Bot):
    if not await ensure_subscribed(msg, bot):
        return
    data = await state.get_data()
    await handle_query(msg, bot, data.get("category"))


@user_router.message(F.text, NOT_CMD)
async def msg_fallback(msg: Message, state: FSMContext, bot: Bot):
    """Holat tanlanmagan bo'lsa ham kod/nom bo'yicha qidiramiz."""
    if await state.get_state() is not None:
        return
    if not await ensure_subscribed(msg, bot):
        return
    await handle_query(msg, bot, None)


# ======================================================================
#  ISHGA TUSHIRISH
# ======================================================================
async def main():
    global BOT_USERNAME
    if BOT_TOKEN.startswith("BOT_TOKEN_NI"):
        sys.exit("❌ BOT_TOKEN kiritilmagan! `export BOT_TOKEN=...` yoki faylning tepasida yozing.")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserTrackMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("%s ishga tushdi: @%s", BRAND, BOT_USERNAME)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("To'xtatildi")