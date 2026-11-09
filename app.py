"""Сервер «Дикая поездка»: мини-приложение, сетевые комнаты и Telegram-бот в одном процессе.

Переменные окружения (файл /etc/wildtrip.env на сервере):
  BOT_TOKEN   токен бота от @BotFather (обязательно)
  PUBLIC_URL  адрес сервера с https://, например https://1-2-3-4.sslip.io
  APP_SHORT   короткое имя мини-приложения в BotFather (по умолчанию Game)
  PORT        порт, на котором слушает сервер (по умолчанию 8080)
  DEV_AUTH=1  только для тестов: вход без подписи Telegram
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import WSMsgType, web

import engine
from engine import Room, new_code

log = logging.getLogger("wildtrip")
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "web")
BOT_TOKEN = "".join((os.getenv("BOT_TOKEN") or "").split())
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
APP_SHORT = os.getenv("APP_SHORT", "Game")
DEV_AUTH = os.getenv("DEV_AUTH") == "1"

ROOMS: dict[str, Room] = {}
SOCKETS: dict[int, set[web.WebSocketResponse]] = {}   # id игрока → открытые соединения
WHERE: dict[int, str] = {}                            # id игрока → код комнаты
BOT_USERNAME = ""


# ---------- проверка, что данные пришли из Telegram ----------
def check_init_data(init_data: str) -> dict | None:
    """Проверка подписи initData по правилам Telegram Mini Apps."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    return user if user.get("id") else None


def send(pid: int, msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False)
    for ws in list(SOCKETS.get(pid, ())):
        if not ws.closed:
            asyncio.get_event_loop().create_task(_safe_send(ws, data))


async def _safe_send(ws, data):
    try:
        await ws.send_str(data)
    except Exception:  # соединение могло закрыться между проверкой и отправкой
        pass


def create_room(host: int) -> Room:
    code = new_code(ROOMS)
    room = Room(code, host, send)
    ROOMS[code] = room
    return room


# ---------- WebSocket ----------
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1_000_000)
    await ws.prepare(request)
    user = None
    pid = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            t = data.get("t")
            if t == "hello":
                user = check_init_data(data.get("init", ""))
                if not user and DEV_AUTH and data.get("dev"):
                    user = data["dev"]
                if not user:
                    await ws.send_json({"t": "fatal", "text": "Открой игру через бота в Telegram."})
                    break
                pid = int(user["id"])
                SOCKETS.setdefault(pid, set()).add(ws)
                name = (user.get("first_name") or user.get("username") or "Путник")[:20]
                code = (data.get("room") or "").upper().strip()
                if code:
                    room = ROOMS.get(code)
                    if not room:
                        await ws.send_json({"t": "error", "text": "Такого лагеря нет или игра уже закончилась. Создай новый."})
                        await ws.send_json({"t": "nolobby"})
                        continue
                    await enter(ws, pid, room, name, user.get("photo_url", ""))
                elif WHERE.get(pid) in ROOMS:
                    await enter(ws, pid, ROOMS[WHERE[pid]], name, user.get("photo_url", ""))
                else:
                    await ws.send_json({"t": "nolobby", "name": name})
                continue
            if pid is None:
                continue
            if t == "create":
                old = ROOMS.get(WHERE.get(pid, ""))
                if old:
                    old.leave(pid)
                room = create_room(pid)
                await enter(ws, pid, room, (user.get("first_name") or "Путник")[:20], user.get("photo_url", ""))
                continue
            if t == "join":
                room = ROOMS.get((data.get("room") or "").upper().strip())
                if not room:
                    await ws.send_json({"t": "error", "text": "Лагерь с таким кодом не найден."})
                    continue
                await enter(ws, pid, room, (user.get("first_name") or "Путник")[:20], user.get("photo_url", ""))
                continue
            room = ROOMS.get(WHERE.get(pid, ""))
            if not room:
                continue
            room.touched = time.time()
            err = None
            if t == "start":
                err = room.start(pid, bool(data.get("bots")))
            elif t == "night":
                err = room.night_action(pid, int(data.get("target", 0)))
            elif t == "mem":
                room.memory(pid, bool(data.get("ok")))
            elif t == "ready":
                room.set_ready(pid)
            elif t == "chat":
                err = room.say(pid, str(data.get("text", "")), str(data.get("img", "")))
            elif t == "contest":
                room.contest_submit(pid, data)
            elif t == "vote":
                err = room.vote(pid, int(data.get("target", -1)))
            elif t == "photo":
                room.set_photo(pid, str(data.get("data", "")))
            elif t == "leave":
                room.leave(pid)
                WHERE.pop(pid, None)
                await ws.send_json({"t": "nolobby"})
            if err:
                await ws.send_json({"t": "error", "text": err})
    finally:
        if pid is not None:
            SOCKETS.get(pid, set()).discard(ws)
            if not SOCKETS.get(pid):
                room = ROOMS.get(WHERE.get(pid, ""))
                if room:
                    p = room.P(pid)
                    if p:
                        p.online = False
                        if room.phase == "lobby":
                            room.leave(pid)
                            WHERE.pop(pid, None)
                        else:
                            room.broadcast()
                            room.maybe_advance()
    return ws


async def enter(ws, pid: int, room: Room, name: str, photo: str) -> None:
    old = WHERE.get(pid)
    if old and old != room.code and old in ROOMS:
        ROOMS[old].leave(pid)
    err = room.join(pid, name, photo)
    if err:
        await ws.send_json({"t": "error", "text": err})
        await ws.send_json({"t": "nolobby"})
        return
    WHERE[pid] = room.code
    link = f"https://t.me/{BOT_USERNAME}/{APP_SHORT}?startapp={room.code}" if BOT_USERNAME else ""
    await ws.send_json({"t": "joined", "code": room.code, "link": link})
    for p in room.players:          # фото всех игроков отдельными сообщениями
        if p.photo:
            await ws.send_json({"t": "photo", "id": p.id, "data": p.photo})
    await ws.send_json({"t": "chat_history", "items": room.chat[-80:]})
    p = room.P(pid)
    if p and p.photo:
        room.broadcast_photo(p)
    room.broadcast()


async def janitor() -> None:
    """Раз в минуту убираем брошенные комнаты; фото игроков удаляются вместе с ними."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for code, room in list(ROOMS.items()):
            online = any(p.online for p in room.players if not p.bot)
            idle = now - room.touched
            if (not online and idle > 600) or idle > 6 * 3600:
                room.stop()
                ROOMS.pop(code, None)
                for p in room.players:
                    if WHERE.get(p.id) == code:
                        WHERE.pop(p.id, None)


# ---------- HTTP ----------
async def index(request: web.Request) -> web.FileResponse:
    resp = web.FileResponse(os.path.join(WEB, "index.html"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "rooms": len(ROOMS), "players": sum(len(r.players) for r in ROOMS.values())})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", health)
    return app


# ---------- Telegram-бот ----------
async def run_bot() -> None:
    global BOT_USERNAME
    from aiogram import Bot, Dispatcher, F, Router
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ChatType, ParseMode
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                               Message, WebAppInfo)

    os.environ["WEBAPP_URL"] = PUBLIC_URL + "/"
    import legacy_bot  # классическая «Мафия» в группе: /mafia, /go, /stop

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    r = Router()

    def app_kb(code: str = "", text: str = "🔥 Войти в лагерь") -> InlineKeyboardMarkup:
        url = f"https://t.me/{me.username}/{APP_SHORT}" + (f"?startapp={code}" if code else "")
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])

    @r.message(Command("play", "app", "camp"))
    async def play(m: Message):
        if m.chat.type == ChatType.PRIVATE:
            await m.answer("Ночь, костёр, степь. Открой игру и создай лагерь для друзей:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                               text="🔥 Играть в «Дикую поездку»", web_app=WebAppInfo(url=PUBLIC_URL + "/"))]]))
            return
        room = create_room(m.from_user.id)
        await m.answer(f"🏕 <b>{m.from_user.first_name}</b> разбил лагерь в степи.\n"
                       f"Жмите кнопку, чтобы присоединиться. Нужно от {engine.MIN_PLAYERS} игроков, "
                       f"недостающих можно добрать ботами.\nКод лагеря: <code>{room.code}</code>",
                       reply_markup=app_kb(room.code))

    @r.message(CommandStart())
    async def start(m: Message, command: CommandObject):
        arg = command.args or ""
        if arg.startswith("join_"):
            await legacy_bot.start_deeplink(m, command)
            return
        await m.answer("Это игра «Дикая поездка»: мафия у костра с конкурсами и тайными заданиями.\n\n"
                       "• Кнопка «Играть» внизу открывает игру.\n"
                       "• В группе напиши /play, и бот позовёт всех в общий лагерь.\n"
                       "• Классическая мафия в чате: /mafia в группе.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                           text="🔥 Играть", web_app=WebAppInfo(url=PUBLIC_URL + "/"))]]))

    dp = Dispatcher()
    dp.include_router(r)
    dp.include_router(legacy_bot.router)
    await bot.set_my_commands([
        BotCommand(command="play", description="Открыть игру «Дикая поездка»"),
        BotCommand(command="mafia", description="Классическая мафия в чате"),
        BotCommand(command="go", description="Начать классическую мафию"),
        BotCommand(command="stop", description="Остановить классическую мафию"),
        BotCommand(command="rules", description="Правила"),
    ])
    if PUBLIC_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=PUBLIC_URL + "/")))
    log.info("Бот @%s запущен, игра: %s", me.username, PUBLIC_URL)
    await dp.start_polling(bot, handle_signals=False)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "127.0.0.1" if not DEV_AUTH else "0.0.0.0", port).start()
    log.info("Сервер игры слушает порт %s", port)
    tasks = [asyncio.create_task(janitor())]
    if BOT_TOKEN:
        tasks.append(asyncio.create_task(run_bot()))
    else:
        log.warning("BOT_TOKEN не задан: бот не запущен, вход в игру только в режиме DEV_AUTH")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
