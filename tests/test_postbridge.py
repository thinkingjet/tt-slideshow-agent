from datetime import datetime, timezone

import pytest

from clip_creator.postbridge import PostBridgeClient, PostBridgeError


class FakeResponse:
    status_code = 200
    is_error = False
    content = b"{}"
    headers = {}
    text = "{}"

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return FakeResponse({"id": "post_123"})


def test_postbridge_requires_api_key():
    with pytest.raises(PostBridgeError):
        PostBridgeClient(api_key=None, base_url="https://api.post-bridge.com/v1")


def test_create_post_sends_expected_payload():
    client = PostBridgeClient(api_key="test", base_url="https://api.post-bridge.com/v1")
    fake = FakeClient()
    client.client = fake
    scheduled_at = datetime(2026, 5, 4, 5, 0, tzinfo=timezone.utc)

    response = client.create_post(
        caption="hello",
        media_id="media_123",
        social_account_ids=[1, 2],
        scheduled_at=scheduled_at,
    )

    assert response == {"id": "post_123"}
    method, path, kwargs = fake.calls[0]
    assert method == "POST"
    assert path == "/posts"
    assert kwargs["json"] == {
        "caption": "hello",
        "media": ["media_123"],
        "social_accounts": [1, 2],
        "processing_enabled": True,
        "scheduled_at": "2026-05-04T05:00:00+00:00",
    }

