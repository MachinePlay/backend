from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from machineplay.schemas import GameStatus, GameStreamEvent


class StartGameRequest(BaseModel):
    white_engine_id: UUID
    black_engine_id: UUID
    runner_id: UUID
    # Specific uploaded versions to play; defaults to each engine's latest.
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None


class StartGameResponse(BaseModel):
    id: UUID
    status: str
    white: UUID
    black: UUID


class RunnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runner_id: UUID
    name: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    github_id: int
    login: str
    name: str | None
    avatar_url: str
    is_admin: bool
    created_at: datetime


class PendingSignupOut(BaseModel):
    """The in-progress GitHub signup waiting for the user to pick a handle."""

    suggested_login: str
    name: str | None
    avatar_url: str


class RegisterRequest(BaseModel):
    login: str


class ApiTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prefix: str
    created_at: datetime
    last_used_at: datetime | None


class EngineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    owner_login: str
    version_count: int = 0


class EngineVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: str
    size_bytes: int
    image_repository: str
    image_digest: str
    created_at: datetime


class EngineDetailOut(BaseModel):
    id: UUID
    name: str
    description: str
    owner_login: str
    created_at: datetime
    versions: list[EngineVersionOut]
    games: list["GameOut"]


class EngineRegisterRequest(BaseModel):
    # URL-safe engine name chosen at upload (machineplay.org/{login}/{name}).
    name: str
    version: str
    # Docker repository the CLI pushed to, e.g. "alice/myengine" (no host/tag).
    repository: str
    # Image digest from `docker push`, e.g. "sha256:…".
    digest: str
    # Image size in bytes (from `docker inspect`), for display.
    size_bytes: int = 0


class EngineRegisterResponse(BaseModel):
    engine_id: UUID
    name: str
    owner_login: str
    version: str
    url: str


class TokenOut(BaseModel):
    token: str


class RegistryTokenOut(BaseModel):
    """Docker Registry v2 token response (`GET /registry/token`)."""

    token: str
    access_token: str
    expires_in: int
    issued_at: str


class LiveStreamEvent(BaseModel):
    game_id: UUID
    event: GameStreamEvent


class GameOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    white_id: UUID
    black_id: UUID
    white_name: str
    black_name: str
    white_version: str | None
    black_version: str | None
    status: GameStatus
    result: str | None
    moves: list[str]
    fen: str
    pgn: str | None
    white_clock: float
    black_clock: float
    created_at: datetime
    ended_at: datetime | None


class UserProfileOut(BaseModel):
    """Public profile: the user plus their engines and those engines' games."""

    login: str
    name: str | None
    avatar_url: str
    created_at: datetime
    engines: list[EngineOut]
    games: list[GameOut]
