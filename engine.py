"""Движок сетевой игры «Дикая поездка».

Сервер хранит всю партию: роли видит только он, клиенты получают персональный вид состояния.
Модуль не знает про WebSocket и Telegram: наружу он сообщает о событиях через room.out(pid, msg).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

BANKS = json.load(open(os.path.join(os.path.dirname(__file__), "banks.json"), encoding="utf-8"))
QBANK, TRUTH, EMOJI, WORDS = BANKS["QBANK"], BANKS["TRUTH"], BANKS["EMOJI"], BANKS["WORDS"]
NIGHT_STORY = BANKS["NIGHT_STORY"]
ROLE_NAMES = {k: v["n"] for k, v in BANKS["ROLES"].items()}
CONTEST_TITLE = BANKS["CONTEST_TITLE"]

SPEED = float(os.getenv("GAME_SPEED", "1"))  # в тестах ускоряем время
T = {"night": 45, "morning": 20, "day": 120, "contest": 45, "vote": 40, "result": 15}
MIN_PLAYERS, MAX_PLAYERS = 5, 12

BOT_NAMES = [("Анна", True), ("Борис", False), ("Вика", True), ("Глеб", False), ("Дима", False),
             ("Ева", True), ("Жора", False), ("Зоя", True), ("Игорь", False), ("Кира", True), ("Лёва", False)]
BADGE_TEXT = {
    "Хамелеон": "Выполнил тайное задание.", "Фотограф": "Выполнил фотозадание.",
    "Зоркий глаз": "Проголосовал против диверсанта.", "Железная память": "Прошёл ночную игру на память.",
    "Ночной гость": "Диверсия прошла успешно.", "Вовремя починил": "Спас игрока от диверсии.",
    "Следопыт-ас": "Нашёл диверсанта по следам.", "Выживший": "Дожил до конца игры.",
    "Чемпион конкурса": "Выиграл конкурс между раундами.", "Знаток трассы": "Без ошибок в викторине.",
    "Детектор лжи": "Без ошибок в «Правде или выдумке».", "Шифровальщик": "Разгадал все поговорки.",
    "Грамотей": "Собрал все слова из букв.", "Калькулятор": "Решил все примеры.",
    "Физиономист": "Узнал всех по фото.",
}
PERFECT_BADGE = {"quiz": "Знаток трассы", "truth": "Детектор лжи", "emoji": "Шифровальщик",
                 "anagram": "Грамотей", "math": "Калькулятор", "photo": "Физиономист"}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐❤]")


def norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def fmt(t: str, speaker: "Player", x: "Player | None") -> str:
    t = re.sub(r"\{([^{}|]*)\|([^{}|]*)\}", lambda m: m.group(2) if speaker.female else m.group(1), t)
    return t.replace("{X}", x.name if x else "")


@dataclass
class Player:
    id: int
    name: str
    bot: bool = False
    female: bool = False
    photo: str = ""            # url Telegram или data:image… (хранится только в памяти комнаты)
    online: bool = False
    alive: bool = True
    role: str = ""
    ledger: list = field(default_factory=list)
    badges: list = field(default_factory=list)
    task: dict | None = None

    @property
    def bad(self) -> bool:
        return self.role == "saboteur"

    def points(self) -> int:
        return sum(n for n, _ in self.ledger)


class Room:
    def __init__(self, code: str, host: int, out: Callable[[int, dict], None]):
        self.code, self.host, self.out = code, host, out
        self.players: list[Player] = []
        self.phase = "lobby"
        self.round = 0
        self.deadline = 0.0
        self.timer: asyncio.Task | None = None
        self.bot_tasks: list[asyncio.Task] = []
        self.used: dict[str, list] = {}
        self.recent_contests: list[str] = []
        self.last_event = ""
        self.chat: list[dict] = []
        self.winner = ""
        self.created = time.time()
        self.touched = time.time()
        self._reset_round()

    # ---------- общие помощники ----------
    def P(self, pid: int) -> Player | None:
        return next((p for p in self.players if p.id == pid), None)

    def alive(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def humans_alive(self) -> list[Player]:
        return [p for p in self.alive() if not p.bot and p.online]

    def check_winner(self) -> str:
        bad = sum(1 for p in self.alive() if p.bad)
        good = len(self.alive()) - bad
        if bad == 0:
            return "camp"
        if bad >= good:
            return "saboteurs"
        return ""

    def add_pts(self, p: Player, n: int, label: str, badge: str = "") -> None:
        p.ledger.append((n, f"Раунд {self.round}: {label}"))
        if badge and badge not in p.badges:
            p.badges.append(badge)
        if not p.bot:
            self.out(p.id, {"t": "toast", "text": f"+{n} очк. {label}"})

    def _reset_round(self) -> None:
        self.night = {"sab": {}, "heal": None, "check": {}, "mem": set(), "done": set(), "info": {}}
        self.morning: dict = {"victim": None, "saved": False, "event": None, "hints": {}}
        self.contest: dict | None = None
        self.votes: dict[int, int] = {}
        self.immune: int | None = None
        self.result: dict = {}
        self.ready: set[int] = set()
        self.chat = []

    def take_fresh(self, n_bank: int, key: str, n: int) -> list[int]:
        used = [i for i in self.used.get(key, []) if i < n_bank]
        free = [i for i in range(n_bank) if i not in used]
        if len(free) < n:
            used, free = [], list(range(n_bank))
        out = random.sample(free, n)
        self.used[key] = used + out
        return out

    # ---------- лобби ----------
    def join(self, pid: int, name: str, photo: str = "") -> str | None:
        self.touched = time.time()
        p = self.P(pid)
        if p:
            p.online = True
            if name:
                p.name = name
            return None
        if self.phase != "lobby":
            # опоздавший смотрит игру как зритель, без роли
            p = Player(pid, name, photo=photo, online=True, alive=False, role="")
            self.players.append(p)
            return None
        if len([q for q in self.players if not q.bot]) >= MAX_PLAYERS:
            return "В лагере уже полно народу."
        self.players.append(Player(pid, self._unique(name), photo=photo, online=True))
        return None

    def _unique(self, name: str) -> str:
        name = (name or "Путник").strip()[:20]
        names = {p.name for p in self.players}
        base, k = name, 2
        while name in names:
            name, k = f"{base} {k}", k + 1
        return name

    def leave(self, pid: int) -> None:
        p = self.P(pid)
        if not p:
            return
        p.online = False
        if self.phase == "lobby":
            self.players.remove(p)
            if self.host == pid and self.players:
                humans = [q for q in self.players if not q.bot]
                if humans:
                    self.host = humans[0].id
        self.maybe_advance()

    def set_photo(self, pid: int, data: str) -> None:
        p = self.P(pid)
        if p and data.startswith(("data:image/", "https://")) and len(data) < 400_000:
            p.photo = data
            self.broadcast_photo(p)

    def start(self, pid: int, fill_bots: bool) -> str | None:
        if pid != self.host:
            return "Начать игру может только тот, кто создал лагерь."
        if self.phase not in ("lobby", "final"):
            return "Игра уже идёт."
        if self.phase == "final":
            self.players = [p for p in self.players if not p.bot and p.online]
        humans = [p for p in self.players if not p.bot]
        need = MIN_PLAYERS - len(humans)
        if need > 0 and not fill_bots:
            return f"Нужно ещё {need} игрок(а). Позови друзей или добери ботами."
        free = [b for b in BOT_NAMES if b[0] not in {p.name for p in self.players}]
        for i in range(max(0, need)):
            nm, fem = free[i]
            self.players.append(Player(-(i + 1), nm, bot=True, female=fem, online=True))
        n = len(self.players)
        sab = 1 if n <= 6 else 2 if n <= 9 else 3
        roles = ["saboteur"] * sab + ["mechanic", "tracker"] + ["tourist"] * (n - sab - 2)
        random.shuffle(roles)
        for p, r in zip(self.players, roles):
            p.role, p.alive, p.ledger, p.badges = r, True, [], []
        self.round = 0
        self.winner = ""
        self.next_round()
        return None

    # ---------- раунды и фазы ----------
    def next_round(self) -> None:
        self.round += 1
        self._reset_round()
        for p in self.alive():
            p.task = None if p.bot else self.make_task(p)
        self.set_phase("night")
        self.bots_night()

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.ready = set()
        secs = T.get(phase, 0) * SPEED
        self.deadline = time.time() + secs if secs else 0
        self.cancel_timer()
        if secs:
            self.timer = asyncio.get_event_loop().create_task(self._timeout(secs, phase, self.round))
        self.broadcast()

    def cancel_timer(self) -> None:
        if self.timer and not self.timer.done() and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None

    def stop(self) -> None:
        self.cancel_timer()
        for t in self.bot_tasks:
            t.cancel()
        self.bot_tasks = []

    async def _timeout(self, secs: float, phase: str, rnd: int) -> None:
        await asyncio.sleep(secs)
        if self.phase == phase and self.round == rnd:
            self.advance()

    def later(self, secs: float, fn) -> None:
        async def run(rnd=self.round, ph=self.phase):
            await asyncio.sleep(secs * SPEED)
            if self.round == rnd and self.phase == ph:
                fn()
        self.bot_tasks.append(asyncio.get_event_loop().create_task(run()))

    def advance(self) -> None:
        ph = self.phase
        if ph == "night":
            self.resolve_night()
            w = self.check_winner()
            if w:
                self.finish(w)
            self.set_phase("final" if w else "morning")
        elif ph == "morning":
            self.set_phase("day")
            self.bots_chat()
        elif ph == "day":
            self.start_contest()
        elif ph == "contest":
            self.finish_contest()
            self.set_phase("vote")
            self.bots_vote()
        elif ph == "vote":
            self.resolve_vote()
            self.set_phase("result")
        elif ph == "result":
            w = self.check_winner()
            if w:
                self.finish(w)
                self.set_phase("final")
            else:
                self.next_round()

    def maybe_advance(self) -> None:
        """Переходим дальше раньше таймера, если все живые люди уже сделали ход."""
        hs = self.humans_alive()
        ph = self.phase
        if ph == "night":
            done = all(p.id in self.night["done"] for p in hs)
        elif ph in ("morning", "day", "result"):
            done = bool(hs) and all(p.id in self.ready for p in hs)
        elif ph == "contest":
            done = bool(self.contest) and all(p.id in self.contest["subs"] for p in hs)
        elif ph == "vote":
            done = all(p.id in self.votes for p in hs)
        else:
            return
        if done or not hs:
            self.advance()

    # ---------- ночь ----------
    def night_action(self, pid: int, target: int) -> str | None:
        p, t = self.P(pid), self.P(target)
        if self.phase != "night" or not p or not p.alive or not t or not t.alive:
            return "Сейчас это сделать нельзя."
        if pid in self.night["done"] and p.role != "tourist":
            return "Ты уже сделал ход этой ночью."
        if p.role == "saboteur":
            if t.bad:
                return "Это твой напарник."
            self.night["sab"][pid] = target
        elif p.role == "mechanic":
            self.night["heal"] = target
        elif p.role == "tracker":
            if target == pid:
                return "Себя проверять незачем."
            self.night["check"][pid] = target
            self.night["info"][pid] = {"id": t.id, "name": t.name, "bad": t.bad}
        else:
            return "У туриста ночью другое занятие."
        self.night["done"].add(pid)
        self.broadcast()
        self.maybe_advance()
        return None

    def memory(self, pid: int, ok: bool) -> None:
        p = self.P(pid)
        if self.phase != "night" or not p or not p.alive or p.role != "tourist" or pid in self.night["done"]:
            return
        if ok:
            self.night["mem"].add(pid)
        self.night["done"].add(pid)
        self.broadcast()
        self.maybe_advance()

    def bots_night(self) -> None:
        al = self.alive()
        for b in [p for p in al if p.bot]:
            if b.role == "saboteur":
                c = [p for p in al if not p.bad]
                if c:
                    self.night["sab"][b.id] = random.choice(c).id
            elif b.role == "mechanic":
                self.night["heal"] = random.choice(al).id
            elif b.role == "tourist" and random.random() < 0.5:
                self.night["mem"].add(b.id)
            self.night["done"].add(b.id)
        if not self.humans_alive():
            self.later(2, self.advance)

    def resolve_night(self) -> None:
        al = self.alive()
        votes = list(self.night["sab"].values())
        if votes:
            top = max(votes.count(v) for v in set(votes))
            target = random.choice([v for v in set(votes) if votes.count(v) == top])
        else:
            target = random.choice([p for p in al if not p.bad]).id
        victim = self.P(target)
        saved = self.night["heal"] == target
        if not saved:
            victim.alive = False
        self.morning.update(victim={"id": victim.id, "name": victim.name, "role": victim.role}, saved=saved)
        for p in al:
            if p.role == "saboteur" and not saved:
                self.add_pts(p, 2, "Диверсия удалась", "Ночной гость")
            if p.role == "mechanic" and saved and self.night["heal"] is not None:
                self.add_pts(p, 3, "Спас игрока", "Вовремя починил")
            if p.role == "tracker" and self.night["info"].get(p.id, {}).get("bad"):
                self.add_pts(p, 2, "Нашёл диверсанта по следам", "Следопыт-ас")
        bad_alive = [p for p in self.alive() if p.bad]
        inno = [p for p in self.alive() if not p.bad]
        for pid in self.night["mem"]:
            p = self.P(pid)
            if p and not p.bot:
                self.add_pts(p, 1, "Железная память", "Железная память")
                cand_bad = [b for b in bad_alive if b.id != pid]
                cand_inn = [x for x in inno if x.id != pid]
                if cand_bad and cand_inn:
                    pair = random.sample([random.choice(cand_bad), random.choice(cand_inn)], 2)
                    self.morning["hints"][pid] = f"Ты слышал шаги у костра. Это были {pair[0].name} или {pair[1].name}."
        self.morning["event"] = self.make_event(bad_alive, inno)

    def make_event(self, bad_alive, inno) -> dict:
        evs = []
        if len(inno) > 1:
            evs.append({"k": "radio", "title": "Ожила рация", "text": f"Сквозь помехи слышно: {random.choice(inno).name} всю ночь на виду у костра, это точно не диверсант."})
        evs.append({"k": "count", "title": "Разведка вернулась", "text": f"Следы показывают: диверсантов в лагере осталось {len(bad_alive)}."})
        if bad_alive and inno:
            a, b = random.sample([random.choice(bad_alive), random.choice(inno)], 2)
            evs.append({"k": "tracks", "title": "Свежие следы", "text": f"Следы у техники ведут к двум палаткам: {a.name} и {b.name}. Кто-то из них точно ходил к стоянке."})
        if bad_alive and len(inno) > 1:
            tr = random.sample([random.choice(bad_alive)] + random.sample(inno, 2), 3)
            evs.append({"k": "glove", "title": "Перчатка у стоянки", "text": f"У техники нашли перчатку в бензине. Такие перчатки у троих: {', '.join(x.name for x in tr)}."})
        evs.append({"k": "fog", "title": "Туман над степью", "text": "Видимость нулевая, никто не видел, что было у соседних палаток. Полагайтесь на слова."})
        evs.append({"k": "find", "title": "Находка в лагере", "text": "В палатке нашли термос с горячим чаем. Всем живым игрокам по очку за бодрость."})
        evs.append({"k": "stars", "title": "Звездопад", "text": "Ночью над степью падали звёзды. Каждый загадал желание: всем живым по очку."})
        evs.append({"k": "convoy", "title": "Чужая колонна", "text": "Под утро мимо лагеря прошла чужая колонна. Шум разбудил всех, но никто ничего толком не разглядел."})
        pool = [e for e in evs if e["k"] != self.last_event] or evs
        ev = random.choice(pool)
        self.last_event = ev["k"]
        if ev["k"] in ("find", "stars"):
            for p in self.alive():
                p.ledger.append((1, f"Раунд {self.round}: {ev['title']}"))
        return ev

    # ---------- задания и чат ----------
    def make_task(self, p: Player) -> dict:
        others = [q for q in self.alive() if q.id != p.id]
        kinds = ["word"] * 3 + ["mention", "question", "short", "photo", "emoji", "number", "long", "ask", "exclaim", "agree"]
        used = self.used.setdefault("task_" + str(p.id), [])
        for _ in range(30):
            k = random.choice(kinds)
            t = None
            if k == "word":
                w, st = random.choice(WORDS)
                t = {"key": "w" + w, "kind": k, "need": 1, "arg": st, "text": f"Вставь в реплику слово «{w}» так, чтобы никто не заподозрил."}
            elif k == "mention" and len(others) >= 3:
                t = {"key": k, "kind": k, "need": 1, "text": "Назови по имени троих игроков в одной реплике."}
            elif k == "question":
                t = {"key": k, "kind": k, "need": 2, "text": "Задай в чате два вопроса: две реплики со знаком «?»."}
            elif k == "short":
                t = {"key": k, "kind": k, "need": 1, "text": "Ответь всего одним словом: реплика из одного слова, от трёх букв."}
            elif k == "photo":
                t = {"key": k, "kind": k, "need": 1, "text": "Фотозадание: прикрепи в чат снимок, который лагерь сочтёт самым смешным."}
            elif k == "emoji":
                t = {"key": k, "kind": k, "need": 2, "text": "Две реплики с эмодзи. Пусть думают, что ты просто весёлый."}
            elif k == "number":
                t = {"key": k, "kind": k, "need": 1, "text": "Упомяни в реплике любое число, например время или километры."}
            elif k == "long":
                t = {"key": k, "kind": k, "need": 1, "text": "Произнеси речь: одна реплика минимум из 12 слов."}
            elif k == "ask" and others:
                x = random.choice(others)
                t = {"key": f"ask{x.id}", "kind": k, "need": 1, "arg": x.name, "text": f"Задай прямой вопрос игроку {x.name}: его имя и знак «?» в одной реплике."}
            elif k == "exclaim":
                t = {"key": k, "kind": k, "need": 2, "text": "Покажи эмоции: две реплики с восклицательным знаком."}
            elif k == "agree":
                t = {"key": k, "kind": k, "need": 1, "text": "Согласись с кем-нибудь: напиши «согласен» или «согласна»."}
            if t and t["key"] not in used:
                used.append(t["key"])
                t.update(prog=0, done=False)
                return t
        used.clear()
        return {"key": "wкарта", "kind": "word", "need": 1, "arg": "карт", "prog": 0, "done": False, "text": "Вставь в реплику слово «карта»."}

    def task_hit(self, p: Player, text: str, img: bool) -> None:
        t = p.task
        if not t or t["done"]:
            return
        m, k = norm(text), t["kind"]
        names = [norm(q.name)[:3] for q in self.alive() if q.id != p.id]
        ok = {
            "word": lambda: t["arg"] in m,
            "mention": lambda: sum(1 for n in names if n in m) >= 3,
            "question": lambda: "?" in text,
            "short": lambda: bool(re.fullmatch(r"\S{3,}", text.strip())),
            "photo": lambda: img,
            "emoji": lambda: bool(EMOJI_RE.search(text)),
            "number": lambda: bool(re.search(r"\d", text)),
            "long": lambda: len(text.split()) >= 12,
            "ask": lambda: norm(t["arg"])[:3] in m and "?" in text,
            "exclaim": lambda: "!" in text,
            "agree": lambda: "соглас" in m,
        }[k]()
        if not ok:
            return
        t["prog"] += 1
        if t["prog"] >= t["need"]:
            t["done"] = True
            self.add_pts(p, 2, "Тайное задание", "Фотограф" if k == "photo" else "Хамелеон")
        self.send_state(p.id)

    def say(self, pid: int, text: str, img: str = "") -> str | None:
        p = self.P(pid)
        if not p:
            return None
        if self.phase not in ("day", "morning", "result", "final", "lobby") or (not p.alive and self.phase not in ("lobby", "final")):
            return "Сейчас говорить нельзя: в лагере ночь или ты выбыл."
        text = (text or "").strip()[:400]
        if img and (not img.startswith("data:image/") or len(img) > 400_000):
            img = ""
        if not text and not img:
            return None
        msg = {"from": p.id, "name": p.name, "text": text, "img": img, "ts": time.time()}
        self.chat.append(msg)
        self.broadcast_all({"t": "chat", "m": msg})
        if self.phase == "day" and p.alive and not p.bot:
            self.task_hit(p, text, bool(img))
            self.bot_reply(p, text)
        return None

    def bots_chat(self) -> None:
        bots = [b for b in self.alive() if b.bot]
        random.shuffle(bots)
        dead = [p for p in self.players if not p.alive and p.role]
        for i, b in enumerate(bots):
            def speak(b=b, i=i):
                if not b.alive:
                    return
                others = [p for p in self.alive() if p.id != b.id]
                cands = [p for p in others if not p.bad] if b.bad else others
                x = random.choice(cands or others)
                bank = BANKS["BOT_ACCUSE"] if b.bad or random.random() < 0.5 else BANKS["BOT_CALM"] + BANKS["BOT_ACCUSE"]
                text = fmt(random.choice(bank), b, x)
                if i == 0 and dead and self.round > 1:
                    d = random.choice(dead)
                    text = f"Жаль, что {d.name} {'выбыла' if d.female else 'выбыл'}. Надо быть осторожнее."
                self.say(b.id, text)
            self.later(3 + i * 5 + random.random() * 3, speak)

    def bot_reply(self, p: Player, text: str) -> None:
        bots = [b for b in self.alive() if b.bot]
        if not bots:
            return
        m = norm(text)
        named = [b for b in bots if norm(b.name)[:3] in m]
        if named:
            who, bank = random.choice(named), BANKS["BOT_DEFEND"]
        elif "?" in text or random.random() < 0.25:
            who, bank = random.choice(bots), BANKS["BOT_ANSWER"]
        else:
            return
        others = [q for q in self.alive() if q.id not in (who.id, p.id)]
        if not others:
            return
        cands = [q for q in others if not q.bad] if who.bad else others
        x = random.choice(cands or others)
        self.later(1.5 + random.random() * 2, lambda: self.say(who.id, fmt(random.choice(bank), who, x)))

    def set_ready(self, pid: int) -> None:
        p = self.P(pid)
        if p and p.alive and self.phase in ("morning", "day", "result"):
            self.ready.add(pid)
            self.broadcast()
            self.maybe_advance()

    # ---------- конкурсы ----------
    def start_contest(self) -> None:
        photo_owners = [p for p in self.players if p.photo and p.role]
        types = ["quiz", "truth", "emoji", "anagram", "math", "react", "stop"]
        if len(photo_owners) >= 3:
            types += ["photo", "photo"]
        fresh = [t for t in types if t not in self.recent_contests] or types
        typ = random.choice(fresh)
        self.recent_contests = (self.recent_contests + [typ])[-3:]
        qs = []
        if typ == "photo":
            for o in random.sample(photo_owners, min(3, len(photo_owners))):
                wrong = random.sample([p.name for p in self.players if p.id != o.id and p.role], 3)
                qs.append(self._q("Чьё это фото? Кадр показан крупным планом.", o.name, wrong, photo=o.id,
                                  zoom=380 + random.randint(0, 120), px=20 + random.randint(0, 60), py=20 + random.randint(0, 60)))
        if typ in ("quiz", "photo"):
            for i in self.take_fresh(len(QBANK), "q", 3 - len(qs)):
                b = QBANK[i]
                qs.append(self._q(b[0], b[1], b[2:]))
        elif typ == "truth":
            for i in self.take_fresh(len(TRUTH), "t", 4):
                s, v = TRUTH[i]
                qs.append({"q": "Правда или выдумка?", "big": s, "small": True, "o": ["Правда", "Выдумка"], "a": 0 if v else 1})
        elif typ == "emoji":
            for i in self.take_fresh(len(EMOJI), "e", 3):
                e, ans = EMOJI[i]
                qs.append(self._q("Какая поговорка зашифрована?", ans, random.sample([x[1] for k, x in enumerate(EMOJI) if k != i], 3), big=e))
        elif typ == "anagram":
            for i in self.take_fresh(len(WORDS), "a", 3):
                w = WORDS[i][0]
                s = w.upper()
                for _ in range(20):
                    s = "".join(random.sample(w.upper(), len(w)))
                    if s != w.upper():
                        break
                qs.append(self._q("Какое слово спряталось в буквах?", w, random.sample([x[0] for k, x in enumerate(WORDS) if k != i], 3), big=s))
        elif typ == "math":
            for _ in range(4):
                op = random.choice("+−×")
                if op == "+":
                    x, y = random.randint(12, 91), random.randint(12, 91); r = x + y
                elif op == "−":
                    x = random.randint(40, 99); y = random.randint(11, x - 10); r = x - y
                else:
                    x, y = random.randint(3, 12), random.randint(4, 12); r = x * y
                wr = []
                for d in random.sample([1, 2, 10, -1, -2, -10, 3, -3], 8):
                    if len(wr) < 3 and r + d > 0:
                        wr.append(str(r + d))
                qs.append(self._q("Сколько будет?", str(r), wr, big=f"{x} {op} {y}"))
        self.contest = {"type": typ, "title": CONTEST_TITLE[typ], "qs": qs, "subs": {}, "scores": {}, "labels": {}, "winner": None, "done": False}
        self.set_phase("contest")
        # боты «играют» в фоне, их результаты появятся в итогах
        for b in [p for p in self.alive() if p.bot]:
            self.contest["subs"][b.id] = time.time() + random.random() * 10

    @staticmethod
    def _q(q, right, wrongs, **kw) -> dict:
        o = [right] + list(wrongs)
        random.shuffle(o)
        return {"q": q, "o": o, "a": o.index(right), **kw}

    def contest_submit(self, pid: int, data: dict) -> None:
        c, p = self.contest, self.P(pid)
        if self.phase != "contest" or not c or not p or not p.alive or pid in c["subs"]:
            return
        typ = c["type"]
        if typ in ("react", "stop"):
            vals = [int(v) for v in (data.get("times") or [])[:3] if isinstance(v, (int, float)) and 0 <= v < 60000]
            if not vals:
                return
            if typ == "react":
                score = max(80, round(sum(vals) / len(vals)))   # меньше быстрее
                c["labels"][pid] = f"{score} мс"
            else:
                score = min(abs(v - 5000) for v in vals)
                c["labels"][pid] = f"±{score / 1000:.2f} с"
        else:
            ans = list(data.get("answers") or [])
            score = sum(1 for q, a in zip(c["qs"], ans) if a == q["a"])
            c["labels"][pid] = f"{score} из {len(c['qs'])}"
        c["scores"][pid] = score
        c["subs"][pid] = time.time()
        self.broadcast()
        self.maybe_advance()

    def finish_contest(self) -> None:
        c = self.contest
        if not c or c["done"]:
            return
        typ, n = c["type"], len(c["qs"])
        lower_better = typ in ("react", "stop")
        for b in [p for p in self.alive() if p.bot]:
            if typ == "react":
                s = 260 + random.randint(0, 420); c["labels"][b.id] = f"{s} мс"
            elif typ == "stop":
                s = 40 + random.randint(0, 650); c["labels"][b.id] = f"±{s / 1000:.2f} с"
            else:
                s = min(n, max(0, round(n * (0.35 + random.random() * 0.55)))); c["labels"][b.id] = f"{s} из {n}"
            c["scores"][b.id] = s
        for p in self.alive():
            if p.id not in c["scores"]:
                c["labels"][p.id] = "не успел"
        sc = {pid: s for pid, s in c["scores"].items() if self.P(pid) and self.P(pid).alive}
        if sc:
            best = min(sc.values()) if lower_better else max(sc.values())
            tops = [pid for pid, s in sc.items() if s == best]
            tops.sort(key=lambda pid: c["subs"].get(pid, 1e18))   # при равенстве выигрывает тот, кто ответил раньше
            c["winner"] = tops[0]
            self.immune = tops[0]
            for pid, s in sc.items():
                p = self.P(pid)
                if typ == "react":
                    pts = 1 if s < 450 else 0
                elif typ == "stop":
                    pts = 1 if s < 300 else 0
                else:
                    pts = s
                    if s == n and PERFECT_BADGE.get(typ) and PERFECT_BADGE[typ] not in p.badges:
                        p.badges.append(PERFECT_BADGE[typ])
                if pts:
                    self.add_pts(p, pts, "Конкурс «" + c["title"] + "»")
            self.add_pts(self.P(tops[0]), 2, "Победа в конкурсе", "Чемпион конкурса")
        c["done"] = True

    # ---------- голосование ----------
    def vote(self, pid: int, target: int) -> str | None:
        p = self.P(pid)
        if self.phase != "vote" or not p or not p.alive:
            return "Сейчас голосовать нельзя."
        if target != -1:
            t = self.P(target)
            if not t or not t.alive or target == pid:
                return "За этого игрока голосовать нельзя."
            if target == self.immune:
                return "У этого игрока иммунитет."
        self.votes[pid] = target
        self.broadcast()
        self.maybe_advance()
        return None

    def bots_vote(self) -> None:
        al = self.alive()
        bad = [p for p in al if p.bad]
        for b in [p for p in al if p.bot]:
            opts = [p for p in al if p.id not in (b.id, self.immune)]
            if not opts:
                continue
            if b.bad:
                t = random.choice([p for p in opts if not p.bad] or opts)
            else:
                hit = [x for x in bad if x in opts]
                t = random.choice(hit) if hit and random.random() < 0.5 else random.choice(opts)
            self.votes[b.id] = t.id
        if not self.humans_alive():
            self.later(2, self.advance)

    def resolve_vote(self) -> None:
        count: dict[int, int] = {}
        for t in self.votes.values():
            if t >= 0:
                count[t] = count.get(t, 0) + 1
        top = max(count.values(), default=0)
        leaders = [k for k, v in count.items() if v == top]
        ex = self.P(leaders[0]) if top and len(leaders) == 1 else None
        for pid, t in self.votes.items():
            tp = self.P(t) if t >= 0 else None
            if tp and tp.bad:
                self.add_pts(self.P(pid), 3, "Голос против диверсанта", "Зоркий глаз")
        if ex:
            ex.alive = False
        self.result = {"count": {str(k): v for k, v in count.items()},
                       "votes": {str(k): v for k, v in self.votes.items()},
                       "executed": {"id": ex.id, "name": ex.name, "role": ex.role} if ex else None}

    def finish(self, w: str) -> None:
        self.winner = w
        for p in self.players:
            if not p.role:
                continue
            if p.alive:
                self.add_pts(p, 2, "Дожил до конца игры", "Выживший")
            if (w == "saboteurs") == p.bad:
                self.add_pts(p, 4, "Победа твоей стороны")
        self.stop()

    # ---------- вид состояния для каждого игрока ----------
    def view(self, pid: int) -> dict:
        me = self.P(pid)
        show_all = self.phase == "final"
        players = []
        for p in self.players:
            role = ""
            if p.role and (show_all or not p.alive or p.id == pid or (me and me.bad and p.bad)):
                role = p.role
            players.append({"id": p.id, "name": p.name, "alive": p.alive, "bot": p.bot, "online": p.online,
                            "role": role, "photo": bool(p.photo), "pts": p.points(), "spect": not p.role})
        v = {"t": "state", "code": self.code, "me": pid, "host": self.host, "phase": self.phase, "round": self.round,
             "left": max(0, round(self.deadline - time.time())) if self.deadline else 0,
             "players": players, "ready": list(self.ready), "story": NIGHT_STORY[(self.round - 1) % len(NIGHT_STORY)] if self.round else ""}
        if me:
            v["my"] = {"role": me.role, "alive": me.alive, "task": {k: me.task[k] for k in ("text", "need", "prog", "done", "kind")} if me.task else None,
                       "pts": me.points(), "ledger": me.ledger, "badges": me.badges}
        if self.phase == "night" and me:
            v["night"] = {"done": pid in self.night["done"],
                          "target": self.night["sab"].get(pid) if me.role == "saboteur" else self.night["heal"] if me.role == "mechanic" else self.night["check"].get(pid),
                          "info": self.night["info"].get(pid), "mates_pick": {str(k): t for k, t in self.night["sab"].items()} if me.bad else {}}
        if self.phase in ("morning", "day") and self.morning["victim"]:
            v["morning"] = {"victim": self.morning["victim"], "saved": self.morning["saved"], "event": self.morning["event"],
                            "hint": self.morning["hints"].get(pid), "info": self.night["info"].get(pid)}
        if self.phase in ("contest", "vote") and self.contest:
            c = self.contest
            v["contest"] = {"type": c["type"], "title": c["title"], "qs": c["qs"], "submitted": pid in c["subs"],
                            "done": c["done"], "labels": {str(k): s for k, s in c["labels"].items()} if c["done"] else {},
                            "winner": c["winner"], "count": sum(1 for k in c["subs"] if self.P(k) and not self.P(k).bot)}
        if self.phase == "vote":
            v["vote"] = {"choice": self.votes.get(pid), "immune": self.immune, "count": len([k for k in self.votes if not self.P(k).bot])}
        if self.phase == "result":
            v["result"] = self.result
        if self.phase == "final":
            rank = sorted([p for p in self.players if p.role], key=lambda p: -p.points())
            v["final"] = {"winner": self.winner, "rounds": self.round,
                          "rank": [{"id": p.id, "name": p.name, "pts": p.points(), "role": p.role} for p in rank],
                          "badge_text": BADGE_TEXT}
        return v

    def send_state(self, pid: int) -> None:
        self.out(pid, self.view(pid))

    def broadcast(self) -> None:
        for p in self.players:
            if not p.bot and p.online:
                self.send_state(p.id)

    def broadcast_all(self, msg: dict) -> None:
        for p in self.players:
            if not p.bot and p.online:
                self.out(p.id, msg)

    def broadcast_photo(self, p: Player) -> None:
        self.broadcast_all({"t": "photo", "id": p.id, "data": p.photo})
        self.broadcast()


def new_code(existing) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        c = "".join(secrets.choice(alphabet) for _ in range(5))
        if c not in existing:
            return c
