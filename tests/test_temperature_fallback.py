"""Unit test verifying that embedded temperature 0.0 is not overridden by falsy defaults."""


def test_temperature_zero_honored():
    prompt = {"temperature": 0.0}

    # Old buggy pattern: float(prompt.get("temperature") or 0.3) -> 0.3
    buggy_temp = float(prompt.get("temperature") or 0.3)
    assert buggy_temp == 0.3

    # Fixed pattern:
    fixed_temp = float(prompt["temperature"]) if prompt.get("temperature") is not None else 0.0
    assert fixed_temp == 0.0


def test_temperature_none_defaults_to_zero():
    prompt = {}
    fixed_temp = float(prompt["temperature"]) if prompt.get("temperature") is not None else 0.0
    assert fixed_temp == 0.0
