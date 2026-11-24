from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, cast
from uuid import UUID, uuid4

from beanie import Document, Indexed, Link
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from machineplay.schemas import GameStatus, HardwareInfo, SearchInfo, UciLine

from app import glicko2


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
    # sha256 hex; the plaintext is shown to the user once and never stored.
    token_hash: Annotated[str, Indexed(unique=True)]
    # Kept for display ("mp_ab12cd…").
    prefix: str
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None


class Engine(UUIDDocument):
    name: str
    description: str = ""
    # Free-form keywords ("python", "mcts"), normalized in app/engines.py.
    tags: list[str] = Field(default_factory=list)
    # owner_login is denormalized for display.
    owner: Link[User]
    owner_login: str
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        # The unique key is on the link's target id, stored at "owner.$id".
        indexes = [
            IndexModel([("owner.$id", ASCENDING), ("name", ASCENDING)], unique=True)
        ]


class RatingCategory(StrEnum):
    BULLET = "bullet"
    BLITZ = "blitz"
    RAPID = "rapid"


class Rating(BaseModel):
    """Glicko-2 on the display scale: `phi` is the rating deviation (RD)."""

    mu: float
    phi: float
    sigma: float
    games: int

    @classmethod
    def initial(cls) -> "Rating":
        return cls(mu=glicko2.MU, phi=glicko2.PHI, sigma=glicko2.SIGMA, games=0)


class RatingChange(BaseModel):
    before: float
    after: float


class EngineVersion(UUIDDocument):
    engine: Link[Engine]
    version: str
    # The runner pulls `{registry_host}/{image_repository}@{image_digest}`,
    # e.g. repository "alice/myengine".
    image_repository: str
    image_digest: str
    size_bytes: int = 0
    # Only categories the version has rated games in.
    ratings: dict[RatingCategory, Rating] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        indexes = [
            IndexModel(
                [("engine.$id", ASCENDING), ("version", ASCENDING)], unique=True
            ),
            "created_at",
        ]


class Runner(UUIDDocument):
    # Survives disconnects; live state is in RunnerConnection (app/runners.py).
    owner: Link[User]
    owner_login: str
    # Seeded from the runner's hostname on first connect; owner-editable after.
    name: str
    description: str = ""
    # Last reported in the runner's Introduction; shown while it is offline.
    max_games: int = 0
    hardware: HardwareInfo | None = None
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None


class Book(UUIDDocument):
    """An opening book: short lines of UCI moves games are dealt to start from.

    Lines start from the standard position, never a FEN: a position book makes
    fastchess send `position fen …`, which the starter engine, the runner's
    board and the frontend's replay can't start from. Seeded from `books/` and
    referenced by `slug`, which survives a re-seed.
    """

    slug: Annotated[str, Indexed(unique=True)]
    name: str
    description: str
    # Attribution for the book's page.
    source: str = ""
    # How deep the lines are walked; a line may be shorter where the book ran out.
    plies: int
    lines: list[list[str]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Game(UUIDDocument):
    white: Link[Engine]
    black: Link[Engine]
    white_name: str
    black_name: str
    # Display string plus the EngineVersion id, which pins the exact image.
    white_version: str
    black_version: str
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    runner_id: UUID | None = None
    tc: str | None = None
    # `tournament_seq` is the game's index in the pairing order (interleaved by
    # round). Pairings are inserted in one batch, so `created_at` can't order them.
    tournament_id: UUID | None = None
    tournament_seq: int | None = None
    # Copied from the tournament, so the unrated-games query needs no join.
    rated: bool = False
    rated_at: datetime | None = None
    white_rating: RatingChange | None = None
    black_rating: RatingChange | None = None
    # The dealt line itself, not a (book, index) reference, so the game still
    # says what it played after the book changes.
    book: str | None = None
    opening_moves: list[str] = Field(default_factory=list)
    # PENDING until schedule_game reserves a runner slot and flips it to PLAYING.
    status: GameStatus = GameStatus.PENDING
    result: str | None = None
    # "time forfeit", "cancelled", "runner disconnected", …; None for a plain finish.
    reason: str | None = None
    moves: list[str] = Field(default_factory=list)
    # Parallel to `moves`; None where the engine said nothing scoreable.
    evals: list[SearchInfo | None] = Field(default_factory=list)
    # The live stream carries the full UCI transcript; only this tail is kept.
    uci_tail: list[UciLine] = Field(default_factory=list)
    fen: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    pgn: str | None = None
    white_clock: float = 0.0
    black_clock: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    # Games load without fetch_links, so read the id off the unfetched DBRef.
    @property
    def white_id(self) -> UUID:
        return cast(UUID, self.white.ref.id)

    @property
    def black_id(self) -> UUID:
        return cast(UUID, self.black.ref.id)

    @property
    def book_plies(self) -> int:
        """How many leading `moves` came from the book."""
        return len(self.opening_moves)

    class Settings:
        # Side indexes cover the engine game stats, the runner one its game
        # lists, the status one the home page; tournament ones back the pairings,
        # status filters and results.
        indexes = [
            "created_at",
            IndexModel([("status", ASCENDING), ("ended_at", DESCENDING)]),
            *(
                IndexModel(
                    [
                        (f"{side}.$id", ASCENDING),
                        ("status", ASCENDING),
                        ("result", ASCENDING),
                        ("created_at", ASCENDING),
                    ]
                )
                for side in ("white", "black")
            ),
            IndexModel(
                [
                    ("runner_id", ASCENDING),
                    ("status", ASCENDING),
                    ("ended_at", ASCENDING),
                ]
            ),
            IndexModel(
                [
                    ("tournament_id", ASCENDING),
                    ("status", ASCENDING),
                    ("ended_at", ASCENDING),
                ]
            ),
            IndexModel(
                [
                    ("tournament_id", ASCENDING),
                    ("tournament_seq", ASCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
            IndexModel(
                [
                    ("tournament_id", ASCENDING),
                    ("status", ASCENDING),
                    ("tournament_seq", ASCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
            IndexModel(
                [
                    ("rated", ASCENDING),
                    ("rated_at", ASCENDING),
                    ("status", ASCENDING),
                    ("ended_at", ASCENDING),
                ]
            ),
        ]


class TournamentFormat(StrEnum):
    # Every pair plays / one engine plays all the others.
    ROUND_ROBIN = "round_robin"
    GAUNTLET = "gauntlet"


class TournamentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


class TournamentParticipant(BaseModel):
    # The version is pinned at creation so a mid-tournament upload can't change
    # which binary plays.
    engine_id: UUID
    engine_name: str
    version_id: UUID
    version: str


class Tournament(UUIDDocument):
    name: str
    creator_id: UUID
    created_by: str
    # Every game plays on this one runner, so hardware is equal.
    runner_id: UUID
    tc: str
    format: TournamentFormat
    rated: bool = False
    # Colors alternate across a pairing's games; odd counts are allowed.
    games_per_pairing: int
    # GAUNTLET only: the participant that plays everyone else.
    gauntlet_head_version_id: UUID | None = None
    # `book_seed` fixes which lines the tournament uses, in what order. No book
    # means deterministic engines replay one game per colour assignment.
    book: str | None = None
    book_seed: int = 0
    participants: list[TournamentParticipant] = Field(default_factory=list)
    status: TournamentStatus = TournamentStatus.RUNNING
    created_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    class Settings:
        # (runner_id, status) backs resume-on-reconnect.
        indexes = [
            "created_at",
            IndexModel([("runner_id", ASCENDING), ("status", ASCENDING)]),
        ]
