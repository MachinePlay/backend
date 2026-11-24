from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app import books
from app.models import TournamentFormat, TournamentStatus
from machineplay.schemas import (
    GameStatus,
    GameStreamEvent,
    HardwareInfo,
    SearchInfo,
    Telemetry,
    UciLine,
)


class StartGameRequest(BaseModel):
    white_engine_id: UUID
    black_engine_id: UUID
    runner_id: UUID
    # Specific uploaded versions to play; defaults to each engine's latest.
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    # "base+inc" in seconds (e.g. "30+0.3"); defaults to the server setting.
    tc: str | None = None
    # Book slug; "none" plays from the initial position.
    book: str = books.DEFAULT_SLUG


class StartGameResponse(BaseModel):
    id: UUID
    white: UUID
    black: UUID


class RunnerOut(BaseModel):
    # `runner_id`, not `id`, to match StartGameRequest.
    runner_id: UUID
    name: str
    description: str
    owner_login: str
    online: bool
    max_games: int
    active_games: int = 0
    last_seen_at: datetime | None = None
    # `telemetry` is the last live sample, present only while online.
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
    # Omitted fields are left unchanged.
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
    tags: list[str]
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
    tags: list[str]
    owner_login: str
    created_at: datetime
    versions: list[EngineVersionOut]
    games: list["GameOut"]


class EngineUpdateRequest(BaseModel):
    # Omitted fields are left unchanged; `tags` replaces the whole list.
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = None


class EngineVersionUpdateRequest(BaseModel):
    # Renaming only moves the label: games and tournaments keep the string they
    # recorded.
    version: str | None = Field(default=None, max_length=64)


class EngineRegisterRequest(BaseModel):
    name: str
    version: str
    # e.g. "alice/myengine", no host or tag.
    repository: str
    digest: str
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
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
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
    # `book_plies`: how many leading `moves` came from the book.
    book: str | None = None
    book_plies: int = 0
    created_at: datetime
    ended_at: datetime | None


class GameDetailOut(GameOut):
    """GameOut plus the per-ply analysis, too big for list responses."""

    # Parallel to `moves`.
    evals: list[SearchInfo | None] = []
    # Empty while the game is live; the SSE stream carries the transcript.
    uci_tail: list[UciLine] = []


class UserProfileOut(BaseModel):
    """Public profile: the user plus their engines and those engines' games."""

    login: str
    name: str | None
    avatar_url: str
    created_at: datetime
    engines: list[EngineOut]
    games: list[GameOut]


class TournamentEntry(BaseModel):
    engine_id: UUID
    # Defaults to the engine's latest.
    version_id: UUID | None = None


class TournamentCreateRequest(BaseModel):
    name: str
    format: TournamentFormat
    # Distinct (engine, version) pairs: one engine may enter at two versions.
    entries: list[TournamentEntry]
    # GAUNTLET only: index into `entries` of the head participant.
    gauntlet_head_index: int | None = None
    games_per_pairing: int = 2
    # Must be online.
    runner_id: UUID
    tc: str | None = None
    # Book slug; "none" plays from the initial position.
    book: str = books.DEFAULT_SLUG


class BookOut(BaseModel):
    """One opening book in the catalog; `lines` is a count."""

    slug: str
    name: str
    description: str
    source: str
    plies: int
    lines: int


class TournamentParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    engine_id: UUID
    engine_name: str
    version_id: UUID
    version: str


class StandingRow(BaseModel):
    """One participant's tally over the finished games, keyed by version_id."""

    engine_id: UUID
    engine_name: str
    version_id: UUID
    version: str
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


class TournamentGameRow(BaseModel):
    """One row of the pairings list: no moves, pgn or fen, since a tournament
    runs to thousands of games and the page polls."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    white_name: str
    black_name: str
    white_version: str
    black_version: str
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    status: GameStatus
    result: str | None
    reason: str | None = None
    created_at: datetime
    ended_at: datetime | None


class TournamentGamesOut(BaseModel):
    """A page of the pairings list, plus how many match the status filter."""

    games: list[TournamentGameRow]
    total: int


class TournamentProgress(BaseModel):
    """How many of the tournament's games sit in each state."""

    total: int
    pending: int
    playing: int
    ended: int
    aborted: int


class TournamentDetailOut(BaseModel):
    id: UUID
    name: str
    format: TournamentFormat
    status: TournamentStatus
    runner_id: UUID
    created_by: str
    tc: str
    games_per_pairing: int
    gauntlet_head_version_id: UUID | None
    # Both None when playing from the initial position.
    book: str | None = None
    book_name: str | None = None
    participants: list[TournamentParticipantOut]
    standings: list[StandingRow]
    progress: TournamentProgress
    # Bounded by the runner's `max_games`.
    live_games: list[GameOut]
    # Rough wallclock left; None once nothing is left to play.
    eta_seconds: float | None = None
    created_at: datetime
    ended_at: datetime | None = None
