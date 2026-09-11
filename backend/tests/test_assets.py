"""Club shirt image URLs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.data.assets import shirt_url
from fplai.rules.constants import Position

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://fantasy.premierleague.com/dist/img/shirts/standard"


class TestShirtUrl:
    def test_an_outfield_shirt_uses_the_plain_variant(self):
        assert shirt_url(3) == f"{BASE}/shirt_3-66.webp"

    @pytest.mark.parametrize("position", [Position.DEF, Position.MID, Position.FWD])
    def test_every_outfield_position_gets_the_same_shirt(self, position):
        assert shirt_url(3, position) == f"{BASE}/shirt_3-66.webp"

    def test_a_goalkeeper_gets_the_underscore_one_variant(self):
        assert shirt_url(3, Position.GKP) == f"{BASE}/shirt_3_1-66.webp"

    def test_the_position_may_be_the_raw_element_type(self):
        """Positions arrive from the API as integers."""
        assert shirt_url(3, 1) == shirt_url(3, Position.GKP)
        assert shirt_url(3, 2) == shirt_url(3)

    def test_a_larger_size_is_available_for_the_detail_sheet(self):
        assert shirt_url(43, size=110) == f"{BASE}/shirt_43-110.webp"

    def test_an_unsupported_size_is_rejected(self):
        with pytest.raises(ValueError, match="size must be one of"):
            shirt_url(3, size=64)

    def test_the_base_url_is_overridable(self):
        assert shirt_url(3, base_url="https://cdn.example/shirts/") == (
            "https://cdn.example/shirts/shirt_3-66.webp"
        )

    def test_the_team_code_is_used_not_the_team_id(self):
        """Arsenal are team id 1 but code 3; using the id would 404."""
        bootstrap = json.loads((FIXTURES / "bootstrap_static.json").read_text())
        arsenal = next(t for t in bootstrap["teams"] if t["short_name"] == "ARS")
        assert arsenal["id"] != arsenal["code"]
        assert shirt_url(arsenal["code"]).endswith(f"shirt_{arsenal['code']}-66.webp")

    def test_every_recorded_club_has_a_distinct_shirt(self):
        bootstrap = json.loads((FIXTURES / "bootstrap_static.json").read_text())
        urls = {shirt_url(t["code"]) for t in bootstrap["teams"]}
        assert len(urls) == len(bootstrap["teams"])
