"""Unit tests for Multi-Model Model Zoo selection and manifest handling."""

from arenamcp.format_profile import FormatProfile
from arenamcp.model_zoo import ModelSelection, ModelSpec, ModelZooClient


def test_model_spec_parsing():
    manifest = {
        "manifest_version": 1,
        "model_id": "UWTempo/ver2",
        "deck": "UWTempo",
        "version": 2,
        "format": {"family": "constructed", "variant": "standard", "deck_size": 60, "singleton": False},
        "commander": None,
        "gate_threshold": 0.60,
        "gauntlet_win_rate": 0.26,
        "deck_counts": {"Island": 7, "Malcolm, Alluring Scoundrel": 4},
    }
    spec = ModelSpec.from_dict(manifest, is_resident=True)
    assert spec.model_id == "UWTempo/ver2"
    assert spec.deck == "UWTempo"
    assert spec.version == 2
    assert spec.format_family == "constructed"
    assert spec.deck_size == 60
    assert spec.is_resident is True
    assert spec.label == "MageZero UWTempo v2"


def test_select_standard_uwtempo():
    profile = FormatProfile(
        family="constructed",
        variant="standard",
        deck_size=60,
        singleton=False,
    )
    # Hero playing canonical UWTempo cards matching reference deck
    from arenamcp.model_zoo import _DEFAULT_UWTEMPO_MANIFEST

    hero_deck = []
    for card, cnt in _DEFAULT_UWTEMPO_MANIFEST["deck_counts"].items():
        hero_deck.extend([card] * cnt)

    selection = ModelZooClient.select(profile, hero_deck)
    assert selection is not None
    assert selection.label == "MageZero UWTempo v2"
    assert selection.similarity >= 0.90


def test_select_brawl_falls_back_when_no_brawl_model():
    profile = FormatProfile(
        family="brawl",
        variant="historic_brawl",
        deck_size=99,
        singleton=True,
        has_command_zone=True,
        commander_names=("The Notary Hobbits",),
    )
    hero_deck = ["The Notary Hobbits", "Forest", "Plains", "Food"] * 10

    # Model Zoo only has constructed 60-card models registered by default
    selection = ModelZooClient.select(profile, hero_deck)
    assert selection is None


def test_select_hypothetical_brawl_model():
    # Register a hypothetical newly trained Hobbit Brawl model in the zoo
    hobbit_manifest = {
        "model_id": "HobbitsBrawl/ver1",
        "deck": "HobbitsBrawl",
        "version": 1,
        "format": {"family": "brawl", "variant": "historic_brawl", "deck_size": 99, "singleton": True},
        "commander": "The Notary Hobbits",
        "gate_threshold": 0.40,
        "gauntlet_win_rate": 0.35,
        "deck_counts": {"The Notary Hobbits": 1, "Forest": 10, "Plains": 10},
    }
    brawl_spec = ModelSpec.from_dict(hobbit_manifest, is_resident=True)
    ModelZooClient._models.append(brawl_spec)

    profile = FormatProfile(
        family="brawl",
        variant="historic_brawl",
        deck_size=99,
        singleton=True,
        has_command_zone=True,
        commander_names=("The Notary Hobbits",),
    )
    hero_deck = ["The Notary Hobbits"] + ["Forest"] * 7 + ["Plains"] * 7

    selection = ModelZooClient.select(profile, hero_deck)
    assert selection is not None
    assert selection.label == "MageZero HobbitsBrawl v1"
    assert selection.model_spec.model_id == "HobbitsBrawl/ver1"

    # Clean up test spec
    ModelZooClient._models.remove(brawl_spec)
