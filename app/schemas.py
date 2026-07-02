from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models import TournamentFormat, TournamentStatus
from machineplay.schemas import GameStatus, GameStreamEvent, HardwareInfo, Telemetry


class StartGameRequest(BaseModel):
    white_engine_id: UUID
    black_engine_id: UUID
    runner_id: UUID
    # Specific uploaded versions to play; defaults to each engine's latest.
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    # Time control "base+inc" in seconds (e.g. "30+0.3"); defaults to the
    # server-wide setting.
    tc: str | None = None


class StartGameResponse(BaseModel):
    id: UUID
    white: UUID
    black: UUID


class RunnerOut(BaseModel):
    # runner_id is the runner's stable doc id (kept as `runner_id`, not `id`, so
    # the frontend and StartGameRequest agree on the field name). `online` is the
    # live status; `active_games` is only meaningful while online.
    runner_id: UUID
    name: str
    description: str
    owner_login: str
    online: bool
    max_games: int
    active_games: int = 0
    last_seen_at: datetime | None = None
    # Static hardware (CPU/RAM); shown even when offline. `telemetry` is the last
    # live utilization sample, present only while online.
    hardware: HardwareInfo | None = None
    telemetry: Telemetry | None = None


class RunnerLiveEvent(BaseModel):
    """One runner's live status, pushed over the /stream/runners SSE feed on
    connect, on each telemetry sample, and on disconnect."""

    runner_id: UUID
    online: bool
    active_games: int
    telemetry: Telemetry | None = None


class RunnerUpdateRequest(BaseModel):
    # Owner-editable metadata. Omitted fields are left unchanged.
    name: str | None = None
    description: str | None = None


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
    white_version: str
    black_version: str
    status: GameStatus
    result: str | None
    reason: str | None = None
    moves: list[str]
    fen: str
    pgn: str | None
    white_clock: float
    black_clock: float
    tc: str | None = None
    runner_id: UUID | None = None
    tournament_id: UUID | None = None
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


class TournamentCreateRequest(BaseModel):
    name: str
    format: TournamentFormat
    # Participants by engine id; each engine's latest version is snapshotted at
    # creation. Must be distinct; between 2 and the participant cap.
    engine_ids: list[UUID]
    # Required for GAUNTLET (must be one of engine_ids); ignored for round robin.
    gauntlet_head_id: UUID | None = None
    # Games each pairing plays (colors alternate). Odd values allowed.
    games_per_pairing: int = 2
    # The runner every game plays on (must be online).
    runner_id: UUID
    # Time control "base+inc"; defaults to the server-wide setting.
    tc: str | None = None


class TournamentParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    engine_id: UUID
    engine_name: str
    version_id: UUID
    version: str


class StandingRow(BaseModel):
    """One participant's tally over the tournament's finished (ENDED) games."""

    engine_id: UUID
    engine_name: str
    played: int
    wins: int
    draws: int
    losses: int
    score: float


class TournamentOut(BaseModel):
    """List-view summary: metadata plus game-progress counts."""

    id: UUID
    name: str
    format: TournamentFormat
    status: TournamentStatus
    runner_id: UUID
    created_by: str
    participant_count: int
    games_total: int
    games_completed: int
    created_at: datetime
    ended_at: datetime | None = None


class TournamentDetailOut(BaseModel):
    id: UUID
    name: str
    format: TournamentFormat
    status: TournamentStatus
    runner_id: UUID
    created_by: str
    tc: str
    games_per_pairing: int
    gauntlet_head_id: UUID | None
    participants: list[TournamentParticipantOut]
    standings: list[StandingRow]
    games: list[GameOut]
    created_at: datetime
    ended_at: datetime | None = None
