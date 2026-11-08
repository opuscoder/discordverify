"""Single-process challenge storage, independent of Discord."""

import math
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TTL = 120
COOLDOWN = 30
MAX_ATTEMPTS = 3


class ChallengeError(Exception):
    """A safe, user-facing challenge error."""


class Verdict(Enum):
    PASSED = "passed"
    WRONG = "wrong"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"


@dataclass
class Challenge:
    nonce: str
    answer: str
    expires_at: float
    attempts: int = MAX_ATTEMPTS


class ChallengeStore:
    """Synchronous operations are atomic within the bot's single event loop.

    Both the user and guild form the key. Nonces make older forms unusable.
    No codes, user messages, or tokens are persisted.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.challenges: dict[tuple[int, int], Challenge] = {}
        self.cooldowns: dict[tuple[int, int], float] = {}

    def issue(self, guild_id: int, user_id: int) -> Challenge:
        now = self.clock()
        self.challenges = {
            key: value for key, value in self.challenges.items()
            if value.expires_at > now
        }
        self.cooldowns = {
            key: expiry for key, expiry in self.cooldowns.items() if expiry > now
        }
        key = (guild_id, user_id)
        if key in self.cooldowns:
            wait = math.ceil(self.cooldowns[key] - now)
            raise ChallengeError(f"Новую капчу можно получить через {wait} сек.")
        if len(self.challenges) >= 5000 or len(self.cooldowns) >= 5000:
            raise ChallengeError("Сейчас слишком много проверок. Попробуйте позже.")
        challenge = Challenge(
            nonce=secrets.token_urlsafe(16),
            answer="".join(secrets.choice(ALPHABET) for _ in range(6)),
            expires_at=now + TTL,
        )
        self.challenges[key] = challenge
        self.cooldowns[key] = now + COOLDOWN
        return challenge

    def check(self, guild_id: int, user_id: int, nonce: str, answer: str) -> Verdict:
        key = (guild_id, user_id)
        challenge = self.challenges.get(key)
        if challenge is None or challenge.nonce != nonce:
            return Verdict.EXPIRED
        if self.clock() >= challenge.expires_at:
            del self.challenges[key]
            return Verdict.EXPIRED
        normalized = answer.strip().upper()
        if secrets.compare_digest(normalized.encode(), challenge.answer.encode()):
            del self.challenges[key]
            return Verdict.PASSED
        challenge.attempts -= 1
        if challenge.attempts == 0:
            del self.challenges[key]
            return Verdict.EXHAUSTED
        return Verdict.WRONG
