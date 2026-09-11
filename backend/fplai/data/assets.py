"""Club shirt image URLs.

The FPL CDN keys shirts by the club's **code**, not its id -- Arsenal are team
id 1 but code 3 -- and serves a separate goalkeeper variant with a `_1` suffix.
Both were confirmed against the live CDN; `resources.premierleague.com` returns
403 for these paths and is not the right host.

    outfield    .../shirt_3-66.webp
    goalkeeper  .../shirt_3_1-66.webp
"""

from __future__ import annotations

from ..config import settings
from ..rules.constants import Position

#: Pixel widths the CDN serves. 66 is the pitch-view size; 110 is for the
#: player detail sheet, where a larger shirt is worth the extra bytes.
SHIRT_SIZES = (66, 110, 220)
DEFAULT_SHIRT_SIZE = 66


def shirt_url(
    team_code: int,
    position: Position | int | None = None,
    *,
    size: int = DEFAULT_SHIRT_SIZE,
    base_url: str | None = None,
) -> str:
    """URL for a club's shirt, using the goalkeeper variant for keepers.

    `team_code` is `teams[].code` from `bootstrap-static/`, never `teams[].id`.

    >>> shirt_url(3)
    'https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_3-66.webp'
    >>> shirt_url(3, Position.GKP)
    'https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_3_1-66.webp'
    """
    if size not in SHIRT_SIZES:
        raise ValueError(f"size must be one of {SHIRT_SIZES}, got {size}")

    base = (base_url or settings.shirt_base_url).rstrip("/")
    variant = "_1" if position is not None and Position(position) is Position.GKP else ""
    return f"{base}/shirt_{team_code}{variant}-{size}.webp"


__all__ = ["DEFAULT_SHIRT_SIZE", "SHIRT_SIZES", "shirt_url"]
