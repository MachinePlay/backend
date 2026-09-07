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
    # Time control "base+inc" in seconds (e.g. "30+0.3"); defaults to the
    # server-wide setting.
    tc: str | None = None
    # Opening book to deal this game's starting line from, by slug. "none"
    # plays from the initial position — which, between two deterministic
    # engines, is the same game every time.
    book: str = books.DEFAULT_SLUG


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
    # Owner-editable metadata. Omitted fields are left unchanged; `tags`
    # replaces the whole list. Renaming moves the engine's URL.
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = None


class EngineVersionUpdateRequest(BaseModel):
    # Owner-editable metadata of one uploaded version. Renaming only moves the
    # label: the image stays pinned by repository+digest, and games/tournaments
    # keep the version string they recorded when they were played.
    version: str | None = Field(default=None, max_length=64)


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
    # Exact version each side played; keys standings by (engine, version).
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
    # The opening book this game was dealt from, and how many leading `moves`
    # came out of it rather than from an engine. Zero for a game that started
    # from the initial position.
    book: str | None = None
    book_plies: int = 0
    created_at: datetime
    ended_at: datetime | None


class GameDetailOut(GameOut):
    """One game's page: everything the list view carries, plus the analysis the
    engines produced. Both fields are per-ply and can run to hundreds of
    entries, so they stay out of the list responses."""

    # Parallel to `moves`: what each mover's search reported, for the eval bars
    # and the analysis view.
    evals: list[SearchInfo | None] = []
    # The end of the UCI conversation, kept when the game finished (empty while
    # it is still being streamed — the SSE stream carries it live).
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
    # Specific uploaded version to enter; defaults to the engine's latest. The
    # chosen version is snapshotted onto the tournament at creation.
    version_id: UUID | None = None


class TournamentCreateRequest(BaseModel):
    name: str
    format: TournamentFormat
    # Participants (engine + optional version). Each (engine, version) pair must
    # be distinct — the same engine may enter at two versions. Between 2 and the
    # participant cap.
    entries: list[TournamentEntry]
    # Required for GAUNTLET: the index into `entries` of the head participant.
    # Ignored for round robin.
    gauntlet_head_index: int | None = None
    # Games each pairing plays (colors alternate). Odd values allowed.
    games_per_pairing: int = 2
    # The runner every game plays on (must be online).
    runner_id: UUID
    # Time control "base+inc"; defaults to the server-wide setting.
    tc: str | None = None
    # Opening book every pairing is dealt from, by slug. "none" plays every
    # game from the initial position, which between deterministic engines makes
    # the whole tournament one game per colour assignment.
    book: str = books.DEFAULT_SLUG


class BookOut(BaseModel):
    """One opening book in the catalog. `lines` is a count, not the lines
    themselves: the browser only needs to name and size the choice."""

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
    """One participant's tally over the tournament's finished (ENDED) games.

    A participant is an (engine, version) pair; version_id is its stable key.
    """

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
    """One pairing in the tournament's game list.

    Deliberately narrower than `GameOut`: no moves, pgn or fen. A tournament
    runs to thousands of games and the page polls while it is running, so the
    list carries only what a row renders. The live boards (`live_games`, capped
    by the runner's slot count) still get the full `GameOut`.
    """

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
    """How many of the tournament's games sit in each state.

    Counted with one aggregate rather than by loading the games, so the page
    stays cheap however many pairings there are.
    """

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
    # The book pairings were dealt from, by slug, with its display name
    # resolved; both None when the tournament plays from the initial position.
    book: str | None = None
    book_name: str | None = None
    participants: list[TournamentParticipantOut]
    standings: list[StandingRow]
    progress: TournamentProgress
    # The games playing right now — what the live boards render. Bounded by the
    # runner's `max_games`, so this stays small however big the tournament is.
    live_games: list[GameOut]
    # Rough wallclock left, from the time control and the runner's slot count;
    # None once nothing is left to play. An estimate, not a promise.
    eta_seconds: float | None = None
    created_at: datetime
    ended_at: datetime | None = None
