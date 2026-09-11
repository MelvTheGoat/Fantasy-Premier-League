"""The FPL rules engine.

Pure rule logic with no I/O: everything here can be exercised from unit tests
without a database or a network call. Anything that needs to know what the FPL
API said belongs in `fplai.data`.
"""

from .autosubs import apply_auto_subs
from .captaincy import CaptaincyOutcome, captain_multiplier, resolve_captaincy
from .chips import (
    available_chips,
    chip_deadline,
    chip_set_for_gameweek,
    expiring_chips,
    validate_chip_usage,
)
from .constants import (
    MAX_BANKED_FREE_TRANSFERS,
    MAX_PLAYERS_PER_CLUB,
    SQUAD_COMPOSITION,
    STARTING_BUDGET,
    TRANSFER_HIT_COST,
    Chip,
    Position,
)
from .pricing import profit, selling_price, squad_selling_value, squad_value
from .scoring import (
    GameweekScore,
    PlayerScore,
    SeasonSummary,
    is_final,
    lockdown_time,
    score_gameweek,
    summarise_season,
)
from .squad import (
    format_formation,
    formation_of,
    is_valid_formation,
    squad_cost,
    validate_formation,
    validate_lineup,
    validate_squad,
)
from .transfers import (
    TransferOutcome,
    apply_transfers,
    build_transfer,
    count_transfers,
    resolve_transfers,
    roll_free_transfers,
    transfer_cost,
)
from .types import (
    ChipUsage,
    Lineup,
    Player,
    PlayerGameweekResult,
    RuleViolation,
    Squad,
    SquadPick,
    Substitution,
    Transfer,
    ValidationResult,
)

__all__ = [
    "CaptaincyOutcome",
    "Chip",
    "ChipUsage",
    "GameweekScore",
    "Lineup",
    "MAX_BANKED_FREE_TRANSFERS",
    "MAX_PLAYERS_PER_CLUB",
    "Player",
    "PlayerGameweekResult",
    "PlayerScore",
    "Position",
    "RuleViolation",
    "SQUAD_COMPOSITION",
    "STARTING_BUDGET",
    "SeasonSummary",
    "Squad",
    "SquadPick",
    "Substitution",
    "TRANSFER_HIT_COST",
    "Transfer",
    "TransferOutcome",
    "ValidationResult",
    "apply_auto_subs",
    "apply_transfers",
    "available_chips",
    "build_transfer",
    "captain_multiplier",
    "chip_deadline",
    "chip_set_for_gameweek",
    "count_transfers",
    "expiring_chips",
    "format_formation",
    "formation_of",
    "is_final",
    "is_valid_formation",
    "lockdown_time",
    "profit",
    "resolve_captaincy",
    "resolve_transfers",
    "roll_free_transfers",
    "score_gameweek",
    "selling_price",
    "squad_cost",
    "squad_selling_value",
    "squad_value",
    "summarise_season",
    "transfer_cost",
    "validate_chip_usage",
    "validate_formation",
    "validate_lineup",
    "validate_squad",
]
