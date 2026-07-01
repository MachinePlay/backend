from datetime import datetime, timezone
from typing import Annotated, cast
from uuid import UUID, uuid4

from beanie import Document, Indexed, Link
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from machineplay.schemas import GameStatus, HardwareInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDDocument(Document):
    id: UUID = Field(default_factory=uuid4)  # type: ignore[assignment]


class User(UUIDDocument):
    github_id: Annotated[int, Indexed(unique=True)]
    login: str
    name: str | None = None
    avatar_url: str = ""
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class ApiToken(UUIDDocument):
    user: Link[User]
    # sha256 hex of the plaintext token; the plaintext is shown to the user once
    # and never stored.
    token_hash: Annotated[str, Indexed(unique=True)]
    # First few chars of the plaintext, kept for display ("mp_ab12cd…").
    prefix: str
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None


class Engine(UUIDDocument):
    name: str
    description: str = ""
    # Engines are namespaced per owner: (owner, name) is unique. owner_login is
    # denormalized for display, mirroring how Game stores white_name/black_name.
    owner: Link[User]
    owner_login: str
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        # The unique key is on the link's target id, stored at "owner.$id".
        indexes = [
            IndexModel([("owner.$id", ASCENDING), ("name", ASCENDING)], unique=True)
        ]


class EngineVersion(UUIDDocument):
    engine: Link[Engine]
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
            IndexModel(
                [("engine.$id", ASCENDING), ("version", ASCENDING)], unique=True
            ),
            "created_at",
        ]


class Runner(UUIDDocument):
    # Durable record of a runner. The in-memory RunnerConnection (app/runners.py)
    # only tracks live state (online presence + scheduling); everything worth
    # keeping across restarts/disconnects lives here. Owner is denormalized the
    # same way Engine denormalizes owner_login for display.
    owner: Link[User]
    owner_login: str
    # Seeded from the runner's hostname on first connect; owner-editable after.
    name: str
    description: str = ""
    # Last capacity the runner reported (intro.max_games); kept for display even
    # while the runner is offline.
    max_games: int = 0
    # Static hardware description last reported in the runner's Introduction;
    # kept for display while offline. Optional so pre-hardware docs still load.
    hardware: HardwareInfo | None = None
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None


class Game(UUIDDocument):
    white: Link[Engine]
    black: Link[Engine]
    white_name: str
    black_name: str
    # Which uploaded version each side played.
    white_version: str
    black_version: str
    status: GameStatus = GameStatus.PLAYING
    result: str | None = None
    moves: list[str] = Field(default_factory=list)
    fen: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    pgn: str | None = None
    white_clock: float = 0.0
    black_clock: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    # The engine ids stay in the API surface (GameOut) as plain UUIDs. Games are
    # loaded without fetch_links, so each side is an unfetched Link: read the
    # DBRef id straight off it (cast because bson types DBRef.id as Any).
    @property
    def white_id(self) -> UUID:
        return cast(UUID, self.white.ref.id)

    @property
    def black_id(self) -> UUID:
        return cast(UUID, self.black.ref.id)

    class Settings:
        indexes = ["created_at"]
