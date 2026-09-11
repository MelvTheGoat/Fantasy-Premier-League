"""Transfers: free-transfer accounting, hits, and applying a transfer to a squad.

One free transfer is granted at each deadline. Unused ones bank, up to five.
Anything beyond the free allowance costs four points. Wildcard and Free Hit
lift the limit for a single gameweek, and -- since 2026/27 -- leave banked free
transfers intact rather than resetting them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER,
    Chip,
    FREE_TRANSFERS_PER_GAMEWEEK,
    MAX_BANKED_FREE_TRANSFERS,
    TRANSFER_HIT_COST,
    UNLIMITED_TRANSFER_CHIPS,
)
from .pricing import selling_price
from .types import RuleViolation, Squad, SquadPick, Transfer


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """The cost of a gameweek's transfers and the free transfers left afterwards."""

    transfers_made: int
    free_transfers_used: int
    hits: int
    """Number of transfers charged at four points each."""
    points_cost: int
    free_transfers_after: int
    """Free transfers banked going into the *next* deadline."""
    unlimited: bool = False

    @property
    def rolled(self) -> bool:
        return self.transfers_made == 0


def transfer_cost(transfers_made: int, free_transfers: int, chip: Chip | None = None) -> int:
    """Points deducted for making this many transfers. Never negative."""
    if chip in UNLIMITED_TRANSFER_CHIPS:
        return 0
    return max(transfers_made - free_transfers, 0) * TRANSFER_HIT_COST


def resolve_transfers(
    transfers_made: int,
    free_transfers: int,
    chip: Chip | None = None,
) -> TransferOutcome:
    """Work out this gameweek's hit and next gameweek's free-transfer balance.

    Under Wildcard or Free Hit the transfers are uncharged and the banked free
    transfers carry over untouched, so a manager who rolled up to three
    transfers still has them the week after playing a chip.
    """
    if transfers_made < 0:
        raise ValueError("transfers_made cannot be negative")
    if free_transfers < 0:
        raise ValueError("free_transfers cannot be negative")

    unlimited = chip in UNLIMITED_TRANSFER_CHIPS

    if unlimited:
        remaining = free_transfers
        used = 0
        hits = 0
        accrue = CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER
    else:
        used = min(transfers_made, free_transfers)
        remaining = free_transfers - used
        hits = transfers_made - used
        accrue = True

    after = remaining + (FREE_TRANSFERS_PER_GAMEWEEK if accrue else 0)
    after = min(after, MAX_BANKED_FREE_TRANSFERS)

    return TransferOutcome(
        transfers_made=transfers_made,
        free_transfers_used=used,
        hits=hits,
        points_cost=hits * TRANSFER_HIT_COST,
        free_transfers_after=after,
        unlimited=unlimited,
    )


def roll_free_transfers(free_transfers: int, transfers_made: int = 0) -> int:
    """Free transfers carried into the next gameweek, capped at the bank limit."""
    return resolve_transfers(transfers_made, free_transfers).free_transfers_after


def build_transfer(
    squad: Squad,
    out_element: int,
    in_element: int,
    prices: dict[int, int],
) -> Transfer:
    """Price up a single swap using the seller's sale price and the buyer's list price."""
    pick = squad.pick(out_element)
    if in_element in squad.elements:
        raise RuleViolation(f"element {in_element} is already in the squad")
    if out_element not in prices:
        raise RuleViolation(f"no current price for outgoing element {out_element}")
    if in_element not in prices:
        raise RuleViolation(f"no current price for incoming element {in_element}")

    return Transfer(
        out_element=out_element,
        in_element=in_element,
        selling_price=selling_price(pick.purchase_price, prices[out_element]),
        purchase_price=prices[in_element],
    )


def apply_transfers(
    squad: Squad,
    transfers: list[Transfer],
    *,
    allow_negative_bank: bool = False,
) -> Squad:
    """Return the squad after these transfers, with the bank updated.

    The incoming player is recorded at the price actually paid, which is what
    the selling-price rule needs the next time they are sold.
    """
    picks = list(squad.picks)
    bank = squad.bank

    for transfer in transfers:
        index = next(
            (i for i, p in enumerate(picks) if p.element == transfer.out_element),
            None,
        )
        if index is None:
            raise RuleViolation(f"cannot sell element {transfer.out_element}: not in the squad")
        if any(p.element == transfer.in_element for p in picks):
            raise RuleViolation(f"element {transfer.in_element} is already in the squad")

        bank += transfer.cash_delta
        picks[index] = SquadPick(
            element=transfer.in_element,
            purchase_price=transfer.purchase_price,
        )

    if bank < 0 and not allow_negative_bank:
        raise RuleViolation(
            f"these transfers overspend the bank by £{-bank / 10:.1f}m"
        )

    return squad.with_picks(picks, bank)


def count_transfers(before: Squad, after: Squad) -> int:
    """How many transfers separate two squads.

    Used to price a Manager decision without having to trust a caller's own
    count, and to check that a Free Hit squad really did revert.
    """
    return len(set(after.elements) - set(before.elements))


__all__ = [
    "TransferOutcome",
    "apply_transfers",
    "build_transfer",
    "count_transfers",
    "resolve_transfers",
    "roll_free_transfers",
    "transfer_cost",
]
