from clip_creator.youtube import canonical_video_url, extract_video_id


def test_extract_video_id_from_shorts_url():
    assert (
        extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share")
        == "dQw4w9WgXcQ"
    )


def test_extract_video_id_from_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_canonical_video_url_uses_shorts_path():
    assert canonical_video_url("dQw4w9WgXcQ") == "https://www.youtube.com/shorts/dQw4w9WgXcQ"

