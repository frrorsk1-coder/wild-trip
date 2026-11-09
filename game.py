"""Чистая логика «Мафии»: без Телеграма, чтобы её было легко тестировать."""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from enum import Enum


class GameError(Exception):
    """Ошибка действия игрока. Текст можно показывать пользователю."""


class Role(str, Enum):
    MAFIA = "mafia"
    DOCTOR = "doctor"
    SHERIFF = "sheriff"
    CIVIL = "civil"


ROLE_TITLES = {
    Role.MAFIA: "🔪 Мафия",
    Role.DOCTOR: "💉 Доктор",
    Role.SHERIFF: "🔎 Комиссар",
    Role.CIVIL: "👤 Мирный житель",
}

ROLE_HINTS = {
    Role.MAFIA: "Каждую ночь вместе с напарниками выбираешь, кого убить. Днём притворяйся мирным.",
    Role.DOCTOR: "Каждую ночь можешь вылечить одного игрока (в том числе себя). "
                 "Одного и того же два раза подряд лечить нельзя.",
    Role.SHERIFF: "Каждую ночь проверяешь одного игрока и узнаёшь, мафия он или нет.",
    Role.CIVIL: "Ночью ты спишь. Днём вычисляй мафию и голосуй.",
}


class Phase(Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"
    VOTE = "vote"
    ENDED = "ended"


@dataclass
class Player:
    id: int
    name: str
    role: Role = Role.CIVIL
    alive: bool = True


@dataclass
class NightResult:
    victim: Player | None   # кого выбрала мафия
    saved: bool             # спас ли доктор
    killed: Player | None   # кто реально погиб


class Game:
    def __init__(self, chat_id: int, host_id: int, min_players: int = 5, max_players: int = 15):
        self.chat_id = chat_id
        self.host_id = host_id
        self.min_players = min_players
        self.max_players = max_players
        self.players: dict[int, Player] = {}
        self.phase = Phase.LOBBY
        self.night_no = 0
        self.last_healed: int | None = None
        self.day_votes: dict[int, int] = {}
        self._reset_night()

    # ---------- состав ----------
    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_by_role(self, role: Role) -> list[Player]:
        return [p for p in self.alive if p.role == role]

    @property
    def alive_mafia(self) -> list[Player]:
        return self.alive_by_role(Role.MAFIA)

    def add_player(self, uid: int, name: str) -> None:
        if self.phase != Phase.LOBBY:
            raise GameError("Игра уже началась.")
        if uid in self.players:
            raise GameError("Ты уже в игре.")
        if len(self.players) >= self.max_players:
            raise GameError("Мест больше нет.")
        self.players[uid] = Player(uid, name)

    def start(self) -> None:
        n = len(self.players)
        if self.phase != Phase.LOBBY:
            raise GameError("Игра уже началась.")
        if n < self.min_players:
            raise GameError(f"Нужно минимум {self.min_players} игроков, сейчас {n}.")
        roles = [Role.MAFIA] * max(1, n // 3)
        if len(roles) < n:
            roles.append(Role.SHERIFF)
        if len(roles) < n:
            roles.append(Role.DOCTOR)
        roles += [Role.CIVIL] * (n - len(roles))
        random.shuffle(roles)
        for player, role in zip(self.players.values(), roles):
            player.role = role
        self.phase = Phase.NIGHT

    def role_summary(self) -> str:
        c = Counter(p.role for p in self.players.values())
        parts = [f"{ROLE_TITLES[Role.MAFIA]}: {c[Role.MAFIA]}"]
        if c[Role.SHERIFF]:
            parts.append(ROLE_TITLES[Role.SHERIFF])
        if c[Role.DOCTOR]:
            parts.append(ROLE_TITLES[Role.DOCTOR])
        if c[Role.CIVIL]:
            parts.append(f"{ROLE_TITLES[Role.CIVIL]}: {c[Role.CIVIL]}")
        return ", ".join(parts)

    # ---------- ночь ----------
    def _reset_night(self) -> None:
        self.mafia_votes: dict[int, int] = {}
        self.doctor_target: int | None = None
        self.doctor_done = False
        self.sheriff_done = False

    def begin_night(self) -> None:
        self.night_no += 1
        self.phase = Phase.NIGHT
        self._reset_night()

    def _actor(self, uid: int, role: Role) -> Player:
        if self.phase != Phase.NIGHT:
            raise GameError("Сейчас не ночь.")
        p = self.players.get(uid)
        if not p or not p.alive or p.role != role:
            raise GameError("Это действие не для тебя.")
        return p

    def _alive_target(self, target: int) -> Player:
        t = self.players.get(target)
        if not t or not t.alive:
            raise GameError("Этот игрок уже выбыл.")
        return t

    def mafia_vote(self, uid: int, target: int) -> Player:
        self._actor(uid, Role.MAFIA)
        if uid in self.mafia_votes:
            raise GameError("Ты уже сделал выбор.")
        t = self._alive_target(target)
        if t.role == Role.MAFIA:
            raise GameError("Нельзя убить своего.")
        self.mafia_votes[uid] = target
        return t

    def doctor_heal(self, uid: int, target: int) -> Player:
        self._actor(uid, Role.DOCTOR)
        if self.doctor_done:
            raise GameError("Ты уже сделал выбор.")
        t = self._alive_target(target)
        if target == self.last_healed:
            raise GameError("Нельзя лечить одного и того же два раза подряд.")
        self.doctor_target = target
        self.doctor_done = True
        return t

    def sheriff_check(self, uid: int, target: int) -> tuple[Player, bool]:
        self._actor(uid, Role.SHERIFF)
        if self.sheriff_done:
            raise GameError("Ты уже сделал выбор.")
        t = self._alive_target(target)
        if target == uid:
            raise GameError("Себя проверять не нужно.")
        self.sheriff_done = True
        return t, t.role == Role.MAFIA

    def night_complete(self) -> bool:
        if any(m.id not in self.mafia_votes for m in self.alive_mafia):
            return False
        if self.alive_by_role(Role.DOCTOR) and not self.doctor_done:
            return False
        if self.alive_by_role(Role.SHERIFF) and not self.sheriff_done:
            return False
        return True

    def resolve_night(self) -> NightResult:
        victim = None
        votes = Counter(self.mafia_votes.values())
        if votes:
            top = max(votes.values())
            victim = self.players[random.choice([t for t, c in votes.items() if c == top])]
        saved = victim is not None and self.doctor_target == victim.id
        killed = victim if (victim and not saved) else None
        if killed:
            killed.alive = False
        self.last_healed = self.doctor_target
        self.phase = Phase.DAY
        return NightResult(victim, saved, killed)

    # ---------- голосование ----------
    def begin_vote(self) -> None:
        self.phase = Phase.VOTE
        self.day_votes = {}

    def cast_vote(self, voter: int, target: int) -> Player | None:
        """target == 0 означает «никого». Возвращает выбранного игрока или None."""
        if self.phase != Phase.VOTE:
            raise GameError("Сейчас не голосование.")
        v = self.players.get(voter)
        if not v or not v.alive:
            raise GameError("Голосовать могут только живые игроки.")
        if target == 0:
            self.day_votes[voter] = 0
            return None
        t = self._alive_target(target)
        if target == voter:
            raise GameError("За себя голосовать нельзя.")
        self.day_votes[voter] = target
        return t

    def vote_complete(self) -> bool:
        return all(p.id in self.day_votes for p in self.alive)

    def resolve_vote(self) -> tuple[Player | None, Counter]:
        tally = Counter(t for t in self.day_votes.values() if t != 0)
        if not tally:
            return None, tally
        top = max(tally.values())
        leaders = [t for t, c in tally.items() if c == top]
        if len(leaders) > 1:
            return None, tally
        executed = self.players[leaders[0]]
        executed.alive = False
        return executed, tally

    # ---------- конец игры ----------
    def winner(self) -> str | None:
        mafia = len(self.alive_mafia)
        others = len(self.alive) - mafia
        if mafia == 0:
            return "town"
        if mafia >= others:
            return "mafia"
        return None
