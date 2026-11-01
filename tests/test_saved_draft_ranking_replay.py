"""Replays must disclose missing evidence and keep scores out of ranking inputs."""

from types import SimpleNamespace

from scripts.replay_saved_draft_rankings import replay_draft


def fixture():
    cards = {
        g: SimpleNamespace(
            name=f"Card {g}", type_line="Creature", mana_cost="{1}{R}", rarity="common", oracle_text=""
        )
        for g in (1, 2, 3)
    }
    draft = dict(
        draft_id="test",
        event="CubeDraft_Test",
        partition="reserved_replay_check",
        picks=[
            dict(
                pack=2,
                pick=3,
                pool_before_ids=["3", "3"],
                coach_pick_id="2",
                offered=[
                    dict(id="1", name="Card 1", dynamic_score=20),
                    dict(id="2", name="Card 2", dynamic_score=40),
                ],
            ),
        ],
    )
    return draft, cards


def test_comparison_only_scores_after_ranker_and_preserves_actual_pool(monkeypatch):
    draft, cards = fixture()
    calls = []

    def ranker(self, pack, pool, pack_no, pick_no):
        calls.append((pack, pool, pack_no, pick_no))
        return [SimpleNamespace(grp_id=g, name=f"Card {g}", score=10) for g in pack]

    monkeypatch.setattr("scripts.replay_saved_draft_rankings.DraftEventDriver._unrated_ranking", ranker)
    result = replay_draft(draft, SimpleNamespace(get_card=cards.get), None)
    assert calls == [([1, 2], [3, 3], 2, 3)]
    assert result["replay_summary"]["mean_raw_gap"] == 20
    assert result["historical_summary"]["mean_raw_gap"] == 0
    draft["picks"][0]["offered"][1]["dynamic_score"] = None
    result = replay_draft(draft, SimpleNamespace(get_card=cards.get), None)
    assert result["replay_summary"]["nonforced_choices"] == 0
    assert result["historical_summary"]["nonforced_choices"] == 0
    assert calls[-1] == calls[0]


def test_missing_pool_metadata_skips_instead_of_inventing_context():
    draft, cards = fixture()
    del cards[3]
    result = replay_draft(draft, SimpleNamespace(get_card=cards.get), None)
    assert result["evaluated_rows"] == 0
    assert result["skipped_rows"] == 1
    assert result["picks"][0]["missing_ids"] == [3]


def test_retail_event_requires_its_primer_instead_of_silently_using_cube_fallback():
    draft, cards = fixture()
    draft["event"] = "QuickDraft_DFT_Test"
    result = replay_draft(draft, SimpleNamespace(get_card=cards.get), None)
    assert result["evaluated_rows"] == 0
    assert result["skipped_rows"] == 1


def test_retail_alternate_printing_and_unrated_basic_keep_original_offered_ids(monkeypatch):
    draft, cards = fixture()
    draft["event"] = "QuickDraft_DFT_Test"
    cards[1].name = "Forest"
    primer_card = SimpleNamespace(grp_id=20, name="Card 2")
    pool_card = SimpleNamespace(grp_id=3, name="Card 3")
    primer = SimpleNamespace(
        card={20: primer_card, 3: pool_card}.get, by_name=lambda: {"card 2": primer_card, "card 3": pool_card}
    )

    def ranker(pack, pool, passed_primer, **kwargs):
        assert pack == [1, 20]
        assert pool == [3, 3]
        assert kwargs["names"][1] == "Forest"
        return [
            SimpleNamespace(grp_id=20, name="Card 2", score=50),
            SimpleNamespace(grp_id=1, name="Forest", score=0),
        ]

    monkeypatch.setattr("scripts.replay_saved_draft_rankings.rank_pack", ranker)
    result = replay_draft(draft, SimpleNamespace(get_card=cards.get), primer)
    assert result["evaluated_rows"] == 1
    assert result["picks"][0]["replay_pick_id"] == "2"
