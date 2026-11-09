"""Телеграм-бот «Мафия» для групповых чатов (aiogram 3)."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.exceptions import TelegramNotFound, TelegramUnauthorizedError
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from game import ROLE_HINTS, ROLE_TITLES, Game, GameError, Phase, Player, Role

log = logging.getLogger("mafia")

# Настройки (можно менять переменными окружения)
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", "5"))
MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "15"))
NIGHT_SECONDS = float(os.getenv("NIGHT_SECONDS", "60"))
DISCUSS_SECONDS = float(os.getenv("DISCUSS_SECONDS", "90"))
VOTE_SECONDS = float(os.getenv("VOTE_SECONDS", "45"))
LOBBY_SECONDS = float(os.getenv("LOBBY_SECONDS", "600"))

RULES = (
    "🎭 <b>Мафия</b>\n\n"
    "Игроки делятся на мафию и мирных жителей. Роли приходят каждому в личные сообщения.\n\n"
    "🌙 <b>Ночь.</b> Мафия выбирает жертву, доктор лечит одного игрока, комиссар проверяет одного игрока.\n"
    "☀️ <b>День.</b> Бот объявляет, кто погиб. Все обсуждают в чате, затем голосуют кнопками. "
    "Игрок с наибольшим числом голосов выбывает (при ничьей не выбывает никто).\n\n"
    "🏆 Город побеждает, когда вся мафия выбыла. Мафия побеждает, когда её не меньше, чем остальных.\n\n"
    "Выбывшие игроки молчат до конца партии."
)

router = Router()

GAMES: dict[int, "Ctx"] = {}      # chat_id -> игра
USER_CHAT: dict[int, int] = {}    # user_id -> chat_id, где он играет


@dataclass
class Ctx:
    bot: Bot
    game: Game
    bot_username: str
    lobby_msg_id: int | None = None
    vote_msg_id: int | None = None
    night_event: asyncio.Event = field(default_factory=asyncio.Event)
    vote_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None


# ---------------------------------------------------------------- утилиты
def esc(s: str) -> str:
    return html.escape(s)


def b(p: Player) -> str:
    return f"<b>{esc(p.name)}</b>"


def user_name(u) -> str:
    return (u.full_name or u.username or str(u.id))[:30]


async def say(ctx: Ctx, text: str, markup: InlineKeyboardMarkup | None = None):
    try:
        return await ctx.bot.send_message(ctx.game.chat_id, text, reply_markup=markup)
    except Exception:
        log.exception("не удалось написать в группу")
        return None


async def dm(ctx: Ctx, uid: int, text: str, markup: InlineKeyboardMarkup | None = None):
    try:
        return await ctx.bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        log.warning("не удалось написать игроку %s", uid)
        return None


def target_kb(prefix: str, chat_id: int, players: list[Player], extra: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in players:
        kb.button(text=p.name, callback_data=f"{prefix}:{chat_id}:{p.id}")
    kb.adjust(2)
    if extra:
        kb.row(InlineKeyboardButton(text="⏭ Никого", callback_data=f"{prefix}:{chat_id}:0"))
    return kb.as_markup()


def lobby_text(game: Game) -> str:
    if game.players:
        names = "\n".join(f"{i}. {esc(p.name)}" for i, p in enumerate(game.players.values(), 1))
    else:
        names = "пока никого"
    return (
        "🎭 <b>Мафия</b>. Набор игроков\n\n"
        f"Игроки ({len(game.players)}/{game.max_players}):\n{names}\n\n"
        f"Нужно минимум {game.min_players}. Нажми «Присоединиться», бот откроется в личке, "
        "нажми там «Запустить» (иначе я не смогу прислать тебе роль)."
    )


def lobby_kb(ctx: Ctx) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Присоединиться",
              url=f"https://t.me/{ctx.bot_username}?start=join_{ctx.game.chat_id}")
    kb.button(text="▶️ Начать игру", callback_data=f"go:{ctx.game.chat_id}")
    kb.adjust(1)
    return kb.as_markup()


async def refresh_lobby(ctx: Ctx) -> None:
    if ctx.lobby_msg_id is None:
        return
    try:
        await ctx.bot.edit_message_text(
            lobby_text(ctx.game), chat_id=ctx.game.chat_id,
            message_id=ctx.lobby_msg_id, reply_markup=lobby_kb(ctx))
    except Exception:
        log.debug("лобби не обновилось", exc_info=True)


def _cancel(task: asyncio.Task | None) -> None:
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


def cleanup(ctx: Ctx) -> None:
    for p in ctx.game.players.values():
        if USER_CHAT.get(p.id) == ctx.game.chat_id:
            del USER_CHAT[p.id]
    if GAMES.get(ctx.game.chat_id) is ctx:
        del GAMES[ctx.game.chat_id]
    _cancel(ctx.watchdog)


async def stop_game(ctx: Ctx, text: str = "🛑 Игра остановлена.") -> None:
    ctx.game.phase = Phase.ENDED
    _cancel(ctx.task)
    cleanup(ctx)
    await say(ctx, text)


async def is_admin(bot: Bot, chat_id: int, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False


async def wait_event(ev: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(ev.wait(), seconds)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------- лобби
async def lobby_watchdog(ctx: Ctx) -> None:
    await asyncio.sleep(LOBBY_SECONDS)
    if ctx.game.phase == Phase.LOBBY:
        cleanup(ctx)
        await say(ctx, "⌛ Набор закрыт: за 10 минут никто не начал игру. Новая игра: /mafia")


async def new_lobby(bot: Bot, chat_id: int, host_id: int) -> Ctx:
    me = await bot.get_me()
    game = Game(chat_id, host_id, min_players=MIN_PLAYERS, max_players=MAX_PLAYERS)
    ctx = Ctx(bot=bot, game=game, bot_username=me.username)
    GAMES[chat_id] = ctx
    sent = await say(ctx, lobby_text(game), lobby_kb(ctx))
    ctx.lobby_msg_id = sent.message_id if sent else None
    ctx.watchdog = asyncio.create_task(lobby_watchdog(ctx))
    return ctx


async def join_game(chat_id: int, uid: int, name: str) -> str:
    """Возвращает ответ игроку (в личку)."""
    ctx = GAMES.get(chat_id)
    if not ctx or ctx.game.phase != Phase.LOBBY:
        return "Эта игра уже началась или закрыта. Вернись в группу и запусти новую: /mafia"
    other = USER_CHAT.get(uid)
    if other is not None and other != chat_id:
        return "Ты уже участвуешь в другой игре."
    try:
        ctx.game.add_player(uid, name)
    except GameError as e:
        return str(e)
    USER_CHAT[uid] = chat_id
    await refresh_lobby(ctx)
    return "✅ Ты в игре! Возвращайся в группу и жди начала. Роль я пришлю сюда."


async def try_start(ctx: Ctx, uid: int) -> str | None:
    """Возвращает текст ошибки или None, если игра запущена."""
    g = ctx.game
    if uid != g.host_id and not await is_admin(ctx.bot, g.chat_id, uid):
        return "Начать игру может тот, кто её создал, или админ чата."
    try:
        g.start()
    except GameError as e:
        return str(e)
    _cancel(ctx.watchdog)
    ctx.task = asyncio.create_task(run_game(ctx))
    return None


# ---------------------------------------------------------------- ход игры
async def run_game(ctx: Ctx) -> None:
    try:
        await announce_start(ctx)
        while True:
            await night(ctx)
            await day(ctx)
            if ctx.game.phase == Phase.ENDED:
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("игра упала")
        await say(ctx, "😵 Что-то пошло не так, игра остановлена. Новая игра: /mafia")
    finally:
        cleanup(ctx)


async def announce_start(ctx: Ctx) -> None:
    g = ctx.game
    names = "\n".join(f"• {esc(p.name)}" for p in g.players.values())
    await say(ctx, f"🎬 <b>Игра началась!</b>\n\nИгроков: {len(g.players)}\n{names}\n\n"
                   f"В игре: {g.role_summary()}.\nРоли отправлены в личные сообщения.")
    mafia = [p for p in g.players.values() if p.role == Role.MAFIA]
    for p in g.players.values():
        text = f"Твоя роль: <b>{ROLE_TITLES[p.role]}</b>\n{ROLE_HINTS[p.role]}"
        if p.role == Role.MAFIA and len(mafia) > 1:
            mates = ", ".join(esc(m.name) for m in mafia if m.id != p.id)
            text += f"\n\nТвои напарники: {mates}"
        if await dm(ctx, p.id, text) is None:
            await say(ctx, f"⚠️ {b(p)}, я не могу написать тебе в личку. Нажми Start у бота!")
    await asyncio.sleep(min(3.0, NIGHT_SECONDS))


async def night(ctx: Ctx) -> None:
    g = ctx.game
    g.begin_night()
    ctx.night_event.clear()
    await say(ctx, f"🌙 <b>Ночь {g.night_no}.</b> Город засыпает.\n"
                   f"Мафия, доктор и комиссар: выбирайте в личных сообщениях ({int(NIGHT_SECONDS)} сек.).")
    cid = g.chat_id
    alive = g.alive
    victims = [p for p in alive if p.role != Role.MAFIA]
    for m in g.alive_mafia:
        await dm(ctx, m.id, "🔪 Кого убьём этой ночью?", target_kb("k", cid, victims))
    for d in g.alive_by_role(Role.DOCTOR):
        heal = [p for p in alive if p.id != g.last_healed]
        await dm(ctx, d.id, "💉 Кого лечим этой ночью?", target_kb("h", cid, heal))
    for s in g.alive_by_role(Role.SHERIFF):
        check = [p for p in alive if p.id != s.id]
        await dm(ctx, s.id, "🔎 Кого проверяем этой ночью?", target_kb("c", cid, check))
    await wait_event(ctx.night_event, NIGHT_SECONDS)


async def day(ctx: Ctx) -> None:
    g = ctx.game
    res = g.resolve_night()
    if res.killed:
        k = res.killed
        text = f"☀️ <b>Наступило утро.</b>\nЭтой ночью убит {b(k)}. Он был: {ROLE_TITLES[k.role]}."
        await dm(ctx, k.id, "💀 Тебя убили. Теперь ты наблюдаешь молча.")
    elif res.saved:
        text = "☀️ <b>Наступило утро.</b>\nМафия выбрала жертву, но доктор её спас. Никто не погиб!"
    else:
        text = "☀️ <b>Наступило утро.</b>\nНочью никто не погиб."
    await say(ctx, text)
    if await check_end(ctx):
        return

    alive_names = ", ".join(esc(p.name) for p in g.alive)
    await say(ctx, f"💬 <b>Обсуждение</b> ({int(DISCUSS_SECONDS)} сек.)\n"
                   f"Живые игроки ({len(g.alive)}): {alive_names}")
    await asyncio.sleep(DISCUSS_SECONDS)

    g.begin_vote()
    ctx.vote_event.clear()
    sent = await say(ctx, f"🗳 <b>Голосование</b> ({int(VOTE_SECONDS)} сек.)\n"
                          "Кого казним? Голосовать могут только живые.",
                     target_kb("v", g.chat_id, g.alive, extra=True))
    ctx.vote_msg_id = sent.message_id if sent else None
    await wait_event(ctx.vote_event, VOTE_SECONDS)

    if ctx.vote_msg_id is not None:
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=g.chat_id, message_id=ctx.vote_msg_id, reply_markup=None)
        except Exception:
            pass
    voters: dict[int, list[str]] = {}
    for voter, target in g.day_votes.items():
        if target:
            voters.setdefault(target, []).append(g.players[voter].name)
    executed, tally = g.resolve_vote()
    lines = [f"• {b(g.players[t])}: {c} ({esc(', '.join(voters[t]))})"
             for t, c in tally.most_common()]
    result = "\n".join(lines) if lines else "Никто не проголосовал."
    if executed:
        result += f"\n\n⚖️ Город казнил {b(executed)}. Он был: {ROLE_TITLES[executed.role]}."
        await dm(ctx, executed.id, "💀 Тебя казнили. Теперь ты наблюдаешь молча.")
    else:
        result += "\n\n🤷 Мнения разошлись, сегодня никого не казнили."
    await say(ctx, f"📊 <b>Итоги голосования</b>\n{result}")
    await check_end(ctx)


async def check_end(ctx: Ctx) -> bool:
    g = ctx.game
    if g.phase == Phase.ENDED:
        return True
    w = g.winner()
    if not w:
        return False
    g.phase = Phase.ENDED
    title = "🏆 <b>Победил город!</b> Вся мафия найдена." if w == "town" \
        else "🏆 <b>Победила мафия!</b> Город пал."
    roles = "\n".join(
        f"{'💀' if not p.alive else '✅'} {esc(p.name)}: {ROLE_TITLES[p.role]}"
        for p in g.players.values())
    await say(ctx, f"{title}\n\n{roles}\n\nЕщё партию? /mafia")
    return True


# ---------------------------------------------------------------- действия игроков
async def act_kill(ctx: Ctx, uid: int, target: int) -> str:
    g = ctx.game
    t = g.mafia_vote(uid, target)
    me = g.players[uid]
    for m in g.alive_mafia:
        if m.id != uid:
            await dm(ctx, m.id, f"🔪 {esc(me.name)} выбрал жертву: {esc(t.name)}")
    if g.night_complete():
        ctx.night_event.set()
    return f"🔪 Ты выбрал: {esc(t.name)}"


async def act_heal(ctx: Ctx, uid: int, target: int) -> str:
    t = ctx.game.doctor_heal(uid, target)
    if ctx.game.night_complete():
        ctx.night_event.set()
    return f"💉 Ты лечишь: {esc(t.name)}"


async def act_check(ctx: Ctx, uid: int, target: int) -> str:
    t, is_mafia = ctx.game.sheriff_check(uid, target)
    if ctx.game.night_complete():
        ctx.night_event.set()
    verdict = "<b>МАФИЯ</b> 🔪" if is_mafia else "<b>не мафия</b> ✅"
    return f"🔎 {esc(t.name)}: {verdict}"


async def act_vote(ctx: Ctx, uid: int, target: int) -> str:
    t = ctx.game.cast_vote(uid, target)
    if ctx.game.vote_complete():
        ctx.vote_event.set()
    return f"Голос принят: {t.name}" if t else "Голос принят: никого"


NIGHT_ACTIONS = {"k": act_kill, "h": act_heal, "c": act_check}


# ---------------------------------------------------------------- хендлеры
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


@router.message(Command("mafia"), GROUP)
async def cmd_mafia(m: Message, bot: Bot):
    if m.chat.id in GAMES:
        await m.reply("Игра в этом чате уже идёт или набирается. Остановить: /stop")
        return
    await new_lobby(bot, m.chat.id, m.from_user.id)


@router.message(Command("go"), GROUP)
async def cmd_go(m: Message):
    ctx = GAMES.get(m.chat.id)
    if not ctx or ctx.game.phase != Phase.LOBBY:
        await m.reply("Сначала набери игроков: /mafia")
        return
    err = await try_start(ctx, m.from_user.id)
    if err:
        await m.reply(err)


@router.message(Command("stop"), GROUP)
async def cmd_stop(m: Message, bot: Bot):
    ctx = GAMES.get(m.chat.id)
    if not ctx:
        await m.reply("Сейчас нет активной игры.")
        return
    if m.from_user.id != ctx.game.host_id and not await is_admin(bot, m.chat.id, m.from_user.id):
        await m.reply("Остановить игру может тот, кто её создал, или админ чата.")
        return
    await stop_game(ctx)


@router.message(Command("rules", "help"))
async def cmd_rules(m: Message):
    await m.answer(RULES + "\n\nНачать: добавь меня в группу и напиши /mafia")


# ---------- Mini App «Дикая поездка» ----------
def webapp_url() -> str:
    """Адрес мини-приложения: из переменной WEBAPP_URL или из файла webapp_url.txt рядом с ботом."""
    url = os.getenv("WEBAPP_URL", "").strip()
    if not url:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp_url.txt")
        if os.path.exists(path):
            url = open(path, encoding="utf-8").read().strip()
    return url if url.startswith("https://") else ""


def play_keyboard() -> InlineKeyboardMarkup | None:
    url = webapp_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 Играть в «Дикую поездку»", web_app=WebAppInfo(url=url))]])


async def send_play(m: Message, bot_username: str) -> None:
    if not webapp_url():
        await m.answer("Мини-приложение ещё не подключено. Положи ссылку на игру в файл webapp_url.txt рядом с ботом и перезапусти его.")
        return
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("Ночь, костёр, степь. Жми кнопку и заходи в лагерь:", reply_markup=play_keyboard())
    else:
        # в группах кнопка web_app не работает, поэтому ведём в личку с ботом
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 Открыть игру", url=f"https://t.me/{bot_username}?start=play")]])
        await m.answer("Игра «Дикая поездка» открывается в личном чате с ботом:", reply_markup=kb)


@router.message(Command("play", "app"))
async def play_cmd(m: Message):
    me = await m.bot.me()
    await send_play(m, me.username)


@router.message(CommandStart(deep_link=True), F.chat.type == ChatType.PRIVATE)
async def start_deeplink(m: Message, command: CommandObject):
    arg = command.args or ""
    if arg == "play":
        me = await m.bot.me()
        await send_play(m, me.username)
        return
    if arg.startswith("join_"):
        try:
            chat_id = int(arg[5:])
        except ValueError:
            await m.answer("Не понял ссылку. Нажми «Присоединиться» в группе ещё раз.")
            return
        await m.answer(await join_game(chat_id, m.from_user.id, user_name(m.from_user)))
        return
    await m.answer(RULES)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_plain(m: Message):
    await m.answer(RULES + "\n\nДобавь меня в группу с друзьями и напиши там /mafia", reply_markup=play_keyboard())


@router.message(Command("mafia", "go"), F.chat.type == ChatType.PRIVATE)
async def private_mafia(m: Message):
    await m.answer("Игра идёт в групповом чате. Добавь меня в группу и напиши там /mafia")


@router.callback_query(F.data.regexp(r"^go:-?\d+$"))
async def cb_go(cb: CallbackQuery):
    ctx = GAMES.get(int(cb.data.split(":")[1]))
    if not ctx or ctx.game.phase != Phase.LOBBY:
        await cb.answer("Игра уже началась или закрыта.", show_alert=True)
        return
    err = await try_start(ctx, cb.from_user.id)
    await cb.answer(err or "Поехали!", show_alert=bool(err))


@router.callback_query(F.data.regexp(r"^[khc]:-?\d+:\d+$"))
async def cb_night(cb: CallbackQuery):
    kind, chat, target = cb.data.split(":")
    ctx = GAMES.get(int(chat))
    if not ctx:
        await cb.answer("Эта игра уже закончилась.", show_alert=True)
        return
    try:
        text = await NIGHT_ACTIONS[kind](ctx, cb.from_user.id, int(target))
    except GameError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text(text)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^v:-?\d+:\d+$"))
async def cb_vote(cb: CallbackQuery):
    _, chat, target = cb.data.split(":")
    ctx = GAMES.get(int(chat))
    if not ctx:
        await cb.answer("Эта игра уже закончилась.", show_alert=True)
        return
    try:
        text = await act_vote(ctx, cb.from_user.id, int(target))
    except GameError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer(text)


# ---------------------------------------------------------------- запуск
async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = "".join((os.getenv("BOT_TOKEN") or "").split())
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Получи токен у @BotFather и запусти start.command ещё раз.")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except (TelegramNotFound, TelegramUnauthorizedError):
        await bot.session.close()
        raise SystemExit("Телеграм не принял токен. Скопируй его заново в @BotFather (/mybots → бот → API Token) и запусти ещё раз.")
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="mafia", description="Новая игра в Мафию"),
        BotCommand(command="go", description="Начать игру"),
        BotCommand(command="play", description="Открыть игру «Дикая поездка»"),
        BotCommand(command="stop", description="Остановить игру"),
        BotCommand(command="rules", description="Правила"),
    ])
    url = webapp_url()
    if url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=url)))
        print(f"Мини-приложение подключено: {url}")
    else:
        print("Мини-приложение не подключено: нет файла webapp_url.txt со ссылкой https://...")
    print(f"Бот @{me.username} запущен. Чтобы остановить, нажми Ctrl+C.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
