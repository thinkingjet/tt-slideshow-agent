from clip_creator.settings import load_settings


def test_default_hook_duration_is_three_seconds(monkeypatch):
    monkeypatch.delenv("CLIP_CREATOR_HOOK_SECONDS", raising=False)

    assert load_settings().hook_seconds == 3.0

