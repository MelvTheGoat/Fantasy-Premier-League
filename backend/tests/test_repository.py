"""Reading the database back as rules-engine types, and locking picks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.data.repository import (
    LockedPicksExist,
    current_gameweek,
    gameweek_average,
    gameweek_averages,
    last_kickoff,
    load_chips_used,
    load_locked_lineup,
    load_locked_squad,
    load_results,
    load_roster,
    load_roster_at_gameweek,
    load_transfers,
    next_gameweek,
    picks_are_locked,
    save_chip_used,
    save_locked_picks,
    save_manager_state,
    save_transfer,
)
from fplai.rules.constants import Chip, Position
from fplai.rules.types import Lineup, Squad, SquadPick

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("bootstrap_static.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=4)
    ingest_players(connection, load("bootstrap_static_gw5.json"), gameweek=5)
    ingest_fixtures(connection, load("fixtures.json"))
    yield connection
    connection.close()


class TestRoster:
    def test_the_fixture_roster_can_form_a_legal_squad(self, db):
        """Guards the recorded payloads: the optimiser tests downstream are
        meaningless if no legal 2/5/5/3 squad exists within budget."""
        from fplai.rules.squad import validate_squad
        from fplai.rules.types import Squad as RulesSquad

        roster = load_roster(db)
        cost = sum(roster[e].price for e in SQUAD_ELEMENTS)
        squad = RulesSquad(
            picks=tuple(SquadPick(e, roster[e].price) for e in SQUAD_ELEMENTS),
            bank=1000 - cost,
        )
        assert cost <= 1000
        assert validate_squad(squad, roster).ok

    def test_the_roster_comes_back_as_rules_players(self, db):
        roster = load_roster(db)
        assert roster[1].position is Position.GKP
        assert roster[4].position is Position.FWD
        assert roster[1].team == 1
        # The GW5 refresh was the last one ingested, so `now_cost` is GW5's.
        assert roster[3].price == 103

    def test_a_historical_roster_uses_that_gameweeks_prices(self, db):
        """A GW4 decision must be costed at GW4 prices, not today's."""
        assert load_roster_at_gameweek(db, 4)[3].price == 100
        assert load_roster_at_gameweek(db, 5)[3].price == 103


class TestGameweekMetadata:
    def test_the_current_and_next_gameweeks_are_read(self, db):
        assert current_gameweek(db) == 4
        assert next_gameweek(db) == 5

    def test_the_official_average_is_read_from_the_api_data(self, db):
        assert gameweek_average(db, 3) == 55

    def test_gameweeks_without_an_average_are_left_out(self, db):
        averages = gameweek_averages(db)
        assert averages[3] == 55.0
        assert 4 not in averages, "the current gameweek has no final average yet"

    def test_the_last_kickoff_is_parsed_for_lockdown(self, db):
        moment = last_kickoff(db, 4)
        assert moment is not None and moment.hour == 19


class TestResults:
    def test_results_come_back_keyed_by_element(self, db):
        ingest_live_gameweek(db, 3, load("event_3_live.json"))
        results = load_results(db, 3)
        assert results[1].minutes == 90
        assert results[1].total_points == 9

    def test_a_double_gameweek_is_summed_into_one_result(self, db):
        ingest_live_gameweek(db, 4, load("event_4_live.json"))
        results = load_results(db, 4)
        assert results[1].minutes == 180
        assert results[1].fixture_count == 2

    def test_an_unfinished_fixture_leaves_the_result_unsettled(self, db):
        """GW4's fixtures have not finished, so the auto-sub rules must wait."""
        ingest_live_gameweek(db, 4, load("event_4_live.json"))
        assert load_results(db, 4)[1].fixtures_finished is False

    def test_a_finished_gameweek_is_settled(self, db):
        ingest_live_gameweek(db, 3, load("event_3_live.json"))
        assert load_results(db, 3)[1].fixtures_finished is True

    def test_a_blank_player_has_no_result_at_all(self, db):
        ingest_live_gameweek(db, 4, load("event_4_live.json"))
        assert 8 not in load_results(db, 4)


#: A legal 2/5/5/3 squad drawn from the fixture roster: £95.6m across six
#: clubs with no more than three from any one of them.
SQUAD_ELEMENTS = (11, 12, 2, 23, 7, 13, 14, 3, 8, 10, 25, 18, 4, 19, 20)

#: Eleven of them in a legal 4-4-2, with the substitute keeper first on the bench.
STARTERS = (11, 2, 23, 7, 13, 3, 8, 10, 25, 4, 19)
BENCH = (12, 14, 18, 20)


class TestLockedPicks:
    def make(self):
        lineup = Lineup(starters=STARTERS, bench=BENCH, captain=4, vice_captain=3)
        squad = Squad(
            picks=tuple(SquadPick(e, 50 + e) for e in STARTERS + BENCH), bank=5
        )
        return lineup, squad

    def test_picks_are_stored_in_squad_order(self, db):
        lineup, squad = self.make()
        save_locked_picks(db, "manager", 4, lineup, squad, prices={})
        rows = db.execute(
            "SELECT player_id, squad_position FROM locked_picks"
            " WHERE model_id='manager' AND gameweek=4 ORDER BY squad_position"
        ).fetchall()
        assert [r["squad_position"] for r in rows] == list(range(1, 16))
        assert [r["player_id"] for r in rows] == list(STARTERS + BENCH)
        assert rows[11]["squad_position"] == 12, "the bench starts at slot 12"

    def test_the_lineup_round_trips(self, db):
        lineup, squad = self.make()
        save_locked_picks(db, "manager", 4, lineup, squad, prices={})
        assert load_locked_lineup(db, "manager", 4) == lineup

    def test_the_squad_round_trips_with_its_purchase_prices(self, db):
        lineup, squad = self.make()
        save_manager_state(db, 4, bank=5, squad_value=1000, free_transfers=1,
                           free_transfers_after=1)
        save_locked_picks(db, "manager", 4, lineup, squad, prices={})
        loaded = load_locked_squad(db, "manager", 4)
        assert loaded.purchase_price(7) == 57
        assert loaded.bank == 5

    def test_the_selling_price_is_stored_alongside_the_purchase_price(self, db):
        lineup, squad = self.make()
        # Element 11 was bought at 61 and is now 65, so it sells for 63.
        save_locked_picks(db, "manager", 4, lineup, squad, prices={11: 65})
        row = db.execute(
            "SELECT purchase_price, selling_price FROM locked_picks"
            " WHERE model_id='manager' AND gameweek=4 AND player_id=11"
        ).fetchone()
        assert (row["purchase_price"], row["selling_price"]) == (61, 63)

    def test_locked_picks_cannot_be_regenerated(self, db):
        """Rewriting a past deadline's picks with hindsight would invalidate
        every result after it, so it is refused outright."""
        lineup, squad = self.make()
        save_locked_picks(db, "manager", 4, lineup, squad, prices={})
        assert picks_are_locked(db, "manager", 4)
        with pytest.raises(LockedPicksExist, match="GW4"):
            save_locked_picks(db, "manager", 4, lineup, squad, prices={})

    def test_the_two_models_lock_independently(self, db):
        lineup, squad = self.make()
        save_locked_picks(db, "manager", 4, lineup, squad, prices={})
        save_locked_picks(db, "best_xi", 4, lineup, squad, prices={})
        assert picks_are_locked(db, "best_xi", 4)
        assert load_locked_lineup(db, "best_xi", 4) == lineup

    def test_a_gameweek_with_no_picks_reads_as_nothing(self, db):
        assert load_locked_lineup(db, "manager", 9) is None
        assert load_locked_squad(db, "manager", 9) is None
        assert not picks_are_locked(db, "manager", 9)

    def test_selection_reasons_are_stored_for_the_detail_sheet(self, db):
        lineup, squad = self.make()
        save_locked_picks(
            db, "best_xi", 4, lineup, squad, prices={},
            reasons={4: "Two fixtures, on penalties, 8.4 projected"},
        )
        row = db.execute(
            "SELECT selection_reason r FROM locked_picks"
            " WHERE model_id='best_xi' AND gameweek=4 AND player_id=4"
        ).fetchone()
        assert "penalties" in row["r"]


class TestManagerRecords:
    def test_a_chip_is_recorded_with_its_set_and_reason(self, db):
        save_chip_used(db, Chip.BENCH_BOOST, gameweek=4, chip_set=1,
                       reason="Double gameweek: bench projects 18 pts")
        used = load_chips_used(db)
        assert used == [type(used[0])(chip=Chip.BENCH_BOOST, gameweek=4)]
        assert used[0].chip_set == 1

    def test_a_transfer_keeps_its_comparison_for_the_frontend(self, db):
        save_transfer(
            db, gameweek=4, out_element=3, in_element=10,
            selling_price_value=101, purchase_price_value=80,
            comparison={"out": {"xpts": 14.2}, "in": {"xpts": 19.8}},
            reason="Saka carrying a knock; Mbeumo has two home fixtures",
            was_hit=False,
        )
        transfers = load_transfers(db, 4)
        assert len(transfers) == 1
        assert transfers[0]["comparison"]["in"]["xpts"] == 19.8
        assert transfers[0]["was_hit"] is False

    def test_manager_state_round_trips(self, db):
        save_manager_state(
            db, 4, bank=12, squad_value=1004, free_transfers=2,
            free_transfers_after=1, transfers_made=2, transfer_cost=0,
            roll_reason=None,
        )
        from fplai.data.repository import load_manager_state

        state = load_manager_state(db, 4)
        assert state["bank"] == 12
        assert state["free_transfers_after"] == 1
