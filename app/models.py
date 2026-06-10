from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from beanie import Document, Indexed
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from machineplay.schemas import GameStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDDocument(Document):
    id: UUID = Field(default_factory=uuid4)  # type: ignore[assignment]


class Engine(UUIDDocument):
    name: str
    description: str = ""
    # Engines are namespaced per owner: (owner_id, name) is unique. owner_login
    # is denormalized for display, mirroring how Game stores white_name/black_name.
    owner_id: UUID
    owner_login: str
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        indexes = [
            IndexModel([("owner_id", ASCENDING), ("name", ASCENDING)], unique=True)
        ]


class EngineVersion(UUIDDocument):
    engine_id: UUID
    version: str
    # Engines are pushed to the docker registry: image_repository is the repo
    # path (e.g. "alice/myengine"), image_digest pins the exact image. The
    # runner pulls `{registry_host}/{image_repository}@{image_digest}`.
    image_repository: str
    image_digest: str
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        indexes = [
            IndexModel([("engine_id", ASCENDING), ("version", ASCENDING)], unique=True),
            "created_at",
        ]


class ApiToken(UUIDDocument):
    user_id: UUID
    # sha256 hex of the plaintext token; the plaintext is shown to the user once
    # and never stored.
    token_hash: Annotated[str, Indexed(unique=True)]
    # First few chars of the plaintext, kept for display ("mp_ab12cd…").
    prefix: str
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None


class User(UUIDDocument):
    github_id: Annotated[int, Indexed(unique=True)]
    login: str
    name: str | None = None
    avatar_url: str = ""
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Game(UUIDDocument):
    white_id: UUID
    black_id: UUID
    white_name: str
    black_name: str
    # Which uploaded version each side played (None on games predating this).
    white_version: str | None = None
    black_version: str | None = None
    status: GameStatus = GameStatus.PLAYING
    result: str | None = None
    moves: list[str] = Field(default_factory=list)
    fen: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    pgn: str | None = None
    white_clock: float = 0.0
    black_clock: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    class Settings:
        indexes = ["created_at"]
