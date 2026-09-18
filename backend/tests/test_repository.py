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


#: The gameweek that is current in the recording, and the one after it.
CURRENT_GAMEWEEK = 3
NEXT_GAMEWEEK = 4


@pytest.fixture
def db():
    """Real recorded reference data, with a second price snapshot a gameweek
    later so the historical-price reads have something to distinguish."""
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("bootstrap_static.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=CURRENT_GAMEWEEK)

    moved = load("bootstrap_static.json")
    for element in moved["elements"]:
        element["now_cost"] += 3
    ingest_players(connection, moved, gameweek=NEXT_GAMEWEEK)

    ingest_fixtures(connection, load("fixtures.json"))
    yield connection
    connection.close()


@pytest.fixture
def synthetic():
    """The constructed blank/double gameweek season, for the cases the real
    fixture list does not contain yet."""
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("synthetic_bootstrap.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=4)
    ingest_fixtures(connection, load("synthetic_fixtures.json"))
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
        assert len(roster) == 43
        keeper = roster[SQUAD_ELEMENTS[0]]
        assert keeper.position is Position.GKP
        assert keeper.team > 0 and keeper.price > 0
        assert {p.position for p in roster.values()} == set(Position)

    def test_a_historical_roster_uses_that_gameweeks_prices(self, db):
        """A past decision must be costed at that gameweek's prices, not today's."""
        element = SQUAD_ELEMENTS[0]
        then = load_roster_at_gameweek(db, CURRENT_GAMEWEEK)[element].price
        now = load_roster_at_gameweek(db, NEXT_GAMEWEEK)[element].price
        assert now == then + 3


class TestGameweekMetadata:
    def test_the_current_and_next_gameweeks_are_read(self, db):
        assert current_gameweek(db) == CURRENT_GAMEWEEK
        assert next_gameweek(db) == NEXT_GAMEWEEK

    def test_the_official_average_is_read_from_the_api_data(self, db):
        assert gameweek_average(db, 1) > 0

    def test_gameweeks_without_an_average_are_left_out(self, db):
        averages = gameweek_averages(db)
        assert averages[1] > 0
        assert 38 not in averages, "an unplayed gameweek has no average yet"

    def test_the_last_kickoff_is_parsed_for_lockdown(self, db):
        moment = last_kickoff(db, CURRENT_GAMEWEEK)
        assert moment is not None and moment.year == 2026


class TestResults:
    def test_results_come_back_keyed_by_element(self, db):
        ingest_live_gameweek(db, CURRENT_GAMEWEEK, load("event_3_live.json"))
        results = load_results(db, CURRENT_GAMEWEEK)
        assert results
        played = [r for r in results.values() if r.minutes > 0]
        assert played and all(r.fixture_count == 1 for r in played)

    def test_a_finished_gameweek_is_settled(self, db):
        """Every GW3 fixture has finished, so the auto-sub rules may act."""
        ingest_live_gameweek(db, CURRENT_GAMEWEEK, load("event_3_live.json"))
        results = load_results(db, CURRENT_GAMEWEEK)
        assert all(r.fixtures_finished for r in results.values())

    def test_a_double_gameweek_is_summed_into_one_result(self, synthetic):
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        results = load_results(synthetic, 4)
        assert results[1].minutes == 180
        assert results[1].fixture_count == 2

    def test_an_unfinished_fixture_leaves_the_result_unsettled(self, synthetic):
        """Mid-gameweek the auto-sub rules must wait, because a player yet to
        kick off looks identical to one who was left out."""
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        assert load_results(synthetic, 4)[1].fixtures_finished is False

    def test_a_blank_player_has_no_result_at_all(self, synthetic):
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        assert 8 not in load_results(synthetic, 4)


#: A legal 2/5/5/3 squad drawn from the recorded players: £66.2m, at most two
#: from any club. Derived by taking the cheapest legal squad from the
#: recording, so it stays valid as long as those players are in it.
SQUAD_ELEMENTS = (58, 355, 38, 280, 451, 609, 423, 50, 438, 213, 547, 600, 107, 528, 322)

#: Eleven of them in a legal 4-4-2, with the substitute keeper first on the bench.
STARTERS = (58, 38, 280, 451, 609, 50, 438, 213, 547, 107, 528)
BENCH = (355, 423, 600, 322)

#: A forward and a midfielder from the XI, for armband assertions.
CAPTAIN = STARTERS[-1]
VICE = STARTERS[-2]


class TestLockedPicks:
    def make(self):
        """A locked lineup whose purchase prices are distinct per player, so a
        round trip that mixed two players up would be visible."""
        lineup = Lineup(
            starters=STARTERS, bench=BENCH, captain=CAPTAIN, vice_captain=VICE
        )
        squad = Squad(
            picks=tuple(SquadPick(e, 40 + index) for index, e in enumerate(STARTERS + BENCH)),
            bank=5,
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
        for index, element in enumerate(STARTERS + BENCH):
            assert loaded.purchase_price(element) == 40 + index
        assert loaded.bank == 5

    def test_the_selling_price_is_stored_alongside_the_purchase_price(self, db):
        lineup, squad = self.make()
        element = STARTERS[0]
        bought = squad.purchase_price(element)
        # A £0.4m rise since purchase: half of it is kept, so it sells for +2.
        save_locked_picks(db, "manager", 4, lineup, squad, prices={element: bought + 4})
        row = db.execute(
            "SELECT purchase_price, selling_price FROM locked_picks"
            " WHERE model_id='manager' AND gameweek=4 AND player_id=?",
            (element,),
        ).fetchone()
        assert (row["purchase_price"], row["selling_price"]) == (bought, bought + 2)

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
            reasons={CAPTAIN: "Two fixtures, on penalties, 8.4 projected"},
        )
        row = db.execute(
            "SELECT selection_reason r FROM locked_picks"
            " WHERE model_id='best_xi' AND gameweek=4 AND player_id=?",
            (CAPTAIN,),
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
            db, gameweek=4, out_element=SQUAD_ELEMENTS[0], in_element=SQUAD_ELEMENTS[1],
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


class TestTheDeadlineIsWhatLocks(TestLockedPicks):
    """A squad is provisional until its deadline and immutable after it. Both
    halves matter: the first lets team news be acted on, the second is the
    no-leakage guarantee the whole project rests on.
    """

    def _gameweek(self, db, gameweek, deadline_time):
        db.execute(
            "INSERT OR REPLACE INTO gameweeks (id, name, deadline_time, updated_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            (gameweek, f"Gameweek {gameweek}", deadline_time),
        )

    def test_a_squad_can_be_rewritten_before_its_deadline(self, db):
        from datetime import UTC, datetime, timedelta

        lineup, squad = self.make()
        now = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
        self._gameweek(db, 8, (now + timedelta(hours=6)).isoformat())

        save_locked_picks(db, "manager", 8, lineup, squad, prices={}, now=now)
        save_locked_picks(db, "manager", 8, lineup, squad, prices={}, now=now)

        stored = db.execute(
            "SELECT COUNT(*) c FROM locked_picks WHERE model_id='manager' AND gameweek=8"
        ).fetchone()["c"]
        assert stored == len(squad.picks), "a rewrite must replace, not accumulate"

    def test_a_squad_cannot_be_rewritten_after_its_deadline(self, db):
        from datetime import UTC, datetime, timedelta

        lineup, squad = self.make()
        now = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
        self._gameweek(db, 9, (now - timedelta(minutes=1)).isoformat())

        save_locked_picks(db, "manager", 9, lineup, squad, prices={}, now=now)
        with pytest.raises(LockedPicksExist):
            save_locked_picks(db, "manager", 9, lineup, squad, prices={}, now=now)

    def test_a_gameweek_with_no_deadline_is_treated_as_closed(self, db):
        """Being unable to prove a write is legitimate is a reason to refuse
        it, not to allow it."""
        lineup, squad = self.make()
        save_locked_picks(db, "manager", 99, lineup, squad, prices={})
        with pytest.raises(LockedPicksExist):
            save_locked_picks(db, "manager", 99, lineup, squad, prices={})
