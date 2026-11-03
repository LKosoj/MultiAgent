import logging

from custom_tools.storybook.studio import media_tokens as mt


def test_issue_and_verify(monkeypatch):
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k1")
    data = mt.issue_media_token("demo", now=1000)
    assert data["expires_at"] == 4600 and data["ttl_seconds"] == 3600
    assert mt.verify_media_token("demo", data["token"], now=1001)


def test_wrong_project_expired_and_forged(monkeypatch):
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k1")
    token = mt.issue_media_token("demo", now=1000)["token"]
    assert not mt.verify_media_token("other", token, now=1001)
    assert not mt.verify_media_token("demo", token, now=4601)
    exp, _, sig = token.partition(".")
    assert not mt.verify_media_token("demo", f"{int(exp) + 100}.{sig}", now=1001)
    assert not mt.verify_media_token("demo", f"{exp}.{'0' * len(sig)}", now=1001)
    # не-ASCII цифры и подпись не должны ронять проверку (раньше было 500)
    for bad in (None, "", "abc", ".x", "x.y", 5, "\u00b2.abc", f"{exp}.\u00e9", "\u0663\u0663.x", "9" * 5000 + ".x"):
        assert not mt.verify_media_token("demo", bad, now=1001)


def test_key_change_invalidates(monkeypatch):
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k1")
    token = mt.issue_media_token("demo", now=1000)["token"]
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k2")
    assert not mt.verify_media_token("demo", token, now=1001)


def test_random_key_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("STORYBOOK_MEDIA_SIGNING_KEY", raising=False)
    monkeypatch.setattr(mt, "_process_key", None)
    with caplog.at_level(logging.WARNING):
        token = mt.issue_media_token("demo", now=1000)["token"]
        mt.issue_media_token("demo", now=1000)
    assert mt.verify_media_token("demo", token, now=1001)
    assert sum("STORYBOOK_MEDIA_SIGNING_KEY" in r.message for r in caplog.records) == 1
