"""Transactional headers use public support without changing delivery or key contents."""

from unittest.mock import MagicMock

import pytest
from tests.test_patreon_litellm import _load_patreon


@pytest.mark.parametrize("sender", [None, "", "Verified sender <verified@example.test>"])
@pytest.mark.parametrize(
    "kind,product,subject,body_fragment",
    [
        ("key", "mtgacoach", "Your mtgacoach license key", "sk-test-delivery"),
        ("code", "mtgacoach", "Your mtgacoach verification code", "123456"),
        ("code", "ArenaOnAir", "Your ArenaOnAir verification code", "123456"),
        ("expired", "mtgacoach", "Your mtgacoach free trial has expired", "https://mtgacoach.com/subscribe"),
        (
            "expired",
            "ArenaOnAir",
            "Your ArenaOnAir free trial has expired",
            "https://mtgacoach.com/subscribe",
        ),
    ],
)
def test_transactional_sender_and_reply_to(
    tmp_path, monkeypatch, sender, kind, product, subject, body_fragment
):
    monkeypatch.delenv("SMTP_FROM", raising=False)
    if sender is not None:
        monkeypatch.setenv("SMTP_FROM", sender)
    monkeypatch.setenv("SMTP_USER", "relay-login@example.test")
    monkeypatch.setenv("SMTP_PASS", "test-password")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    patreon, _db = _load_patreon(tmp_path, monkeypatch)
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    monkeypatch.setattr(patreon.smtplib, "SMTP", smtp_factory)

    if kind == "key":
        patreon._send_email_sync("recipient@example.test", "Pat", "sk-test-delivery")
    elif kind == "code":
        patreon.send_trial_code_email("recipient@example.test", "123456", product=product)
    else:
        patreon.send_trial_expired_email("recipient@example.test", product=product)

    smtp_factory.assert_called_once_with("smtp.example.test", 587, timeout=20)
    smtp.starttls.assert_called_once_with()
    smtp.login.assert_called_once_with("relay-login@example.test", "test-password")
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert str(message["From"]) == (sender or "support@mtgacoach.com")
    assert str(message["Reply-To"]) == "support@mtgacoach.com"
    assert str(message["To"]) == "recipient@example.test"
    assert str(message["Subject"]) == subject
    assert body_fragment in message.get_content()
