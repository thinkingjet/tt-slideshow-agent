from clip_creator.cli import build_parser


def test_cli_accepts_enqueue_arguments():
    parser = build_parser()
    args = parser.parse_args(
        [
            "enqueue",
            "--url",
            "https://www.youtube.com/@creator/shorts",
            "--cta",
            "cta.mp4",
            "--limit",
            "25",
        ]
    )

    assert args.command == "enqueue"
    assert args.limit == 25
    assert args.cta == "cta.mp4"

