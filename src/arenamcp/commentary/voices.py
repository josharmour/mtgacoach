"""Role pairs from supported voice IDs, independent of old troubleshooting values."""

from arenamcp.kokoro_voices import KOKORO_VOICES


class VoicePairUnavailable(ValueError):
    pass


def pair_options(settings, catalog=None):
    provider = settings.get("tts_provider", "kokoro")
    if provider == "kokoro":
        labels = dict(KOKORO_VOICES)
        lead = settings.get("voice", "am_adam")
        if lead not in labels:
            lead = "am_adam"
        partner = "af_heart" if lead != "af_heart" else "am_adam"
        default = (lead, partner)
        choices = [default, (partner, lead)]
        label = f"Automatic · {labels[lead]} + {labels[partner]}"
    else:
        labels = {"atlas": "Atlas", "mira": "Mira", "default": "Original", "server_default": "Server default"}
        if provider == "chatterbox_remote":
            if not isinstance(catalog, dict):
                raise VoicePairUnavailable("Loading announcer voices from the speech server…")
            available = catalog.get("voices", [])
            lead = catalog.get("default")
            if lead not in available or lead not in {"atlas", "mira", "default"}:
                raise VoicePairUnavailable(
                    "The server must advertise its default voice to choose a distinct partner."
                )
            partner = next((v for v in ("mira", "atlas") if v in available and v != lead), None)
            if partner is None:
                raise VoicePairUnavailable("The speech server needs two distinct announcer voices.")
            default = ("server_default", partner)
            label = f"Server default ({labels[lead]}) + {labels[partner]}"
            choices = [
                (a, b) for a, b in (("atlas", "mira"), ("mira", "atlas")) if a in available and b in available
            ]
        else:
            default, choices = ("atlas", "mira"), [("atlas", "mira"), ("mira", "atlas")]
            label = "Automatic · Atlas + Mira"
    return [("", label, default)] + [("|".join(p), " + ".join(labels[v] for v in p), p) for p in choices]


def voice_pair(settings, catalog=None):
    key = (
        "commentary_kokoro_pair"
        if settings.get("tts_provider", "kokoro") == "kokoro"
        else "commentary_chatterbox_pair"
    )
    selected = settings.get(key) or ""
    choices = pair_options(settings, catalog)
    for value, _label, pair in choices:
        if value == selected:
            return pair
    raise VoicePairUnavailable(
        "That announcer pair is unavailable. Choose an available pair in Speech & voices."
    )
