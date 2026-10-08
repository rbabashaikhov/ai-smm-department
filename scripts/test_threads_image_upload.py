from __future__ import annotations

import argparse

from ai_smm.publishing.threads import ThreadsPublisher


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a local image to public storage "
            "and publish it to Threads."
        )
    )

    parser.add_argument(
        "image",
        help="Path to a local image.",
    )

    parser.add_argument(
        "--text",
        default="AI SMM Department: image publishing test",
        help="Threads post text.",
    )

    parser.add_argument(
        "--alt-text",
        default="AI SMM Department test image",
        help="Image alt text.",
    )

    args = parser.parse_args()

    publisher = ThreadsPublisher()

    result = publisher.create_image_post_from_file(
        local_path=args.image,
        text=args.text,
        alt_text=args.alt_text,
    )

    print()
    print("=== RESULT ===")
    print(result)


if __name__ == "__main__":
    main()