"""Player prices and selling prices.

FPL's selling-price rule: a player sells for what you paid plus half of any
rise since, rounded down to the nearest £0.1m. Falls are taken in full, so a
player whose price drops sells for the new, lower price.

Because prices are held in tenths of a million, "round down to the nearest
£0.1m" is exactly integer floor division of the rise by two -- no float
rounding is involved, which matters because a single tenth of error compounds
across a season of transfers and eventually makes a squad look affordable when
it is not.
"""

from __future__ import annotations

from .types import Squad


def selling_price(purchase_price: int, current_price: int) -> int:
    """Price a player sells for, in tenths of a million.

    >>> selling_price(70, 73)   # bought £7.0m, now £7.3m -> keep half the £0.3m rise
    71
    >>> selling_price(70, 74)   # a £0.4m rise splits evenly
    72
    >>> selling_price(70, 68)   # falls are taken in full
    68
    """
    if purchase_price < 0 or current_price < 0:
        raise ValueError("prices must be non-negative")
    rise = current_price - purchase_price
    if rise <= 0:
        return current_price
    return purchase_price + rise // 2


def profit(purchase_price: int, current_price: int) -> int:
    """Money released by selling, over and above what was paid. Never negative."""
    return max(selling_price(purchase_price, current_price) - purchase_price, 0)


def squad_selling_value(squad: Squad, prices: dict[int, int]) -> int:
    """What the whole squad would sell for right now, in tenths, bank excluded."""
    return sum(selling_price(p.purchase_price, prices[p.element]) for p in squad.picks)


def squad_value(squad: Squad, prices: dict[int, int]) -> int:
    """Selling value plus the bank -- the number FPL shows as squad value."""
    return squad_selling_value(squad, prices) + squad.bank


__all__ = ["profit", "selling_price", "squad_selling_value", "squad_value"]
