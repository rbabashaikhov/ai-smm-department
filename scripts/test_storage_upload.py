from __future__ import annotations

import argparse
from pathlib import Path

from ai_smm.storage import StorageUploader


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a local file to AI SMM public storage."
    )

    parser.add_argument(
        "local_path",
        help="Local file to upload.",
    )

    parser.add_argument(
        "--remote-path",
        required=True,
        help=(
            "Relative remote path, for example "
            "'ai-smm/tests/test-image.jpg'."
        ),
    )

    args = parser.parse_args()

    local_path = Path(args.local_path)

    uploader = StorageUploader()

    print("Uploading...")
    print(f"Local file:  {local_path}")
    print(f"Remote path: {args.remote_path}")

    public_url = uploader.upload(
        local_path=local_path,
        remote_path=args.remote_path,
    )

    print()
    print("Upload successful.")
    print(f"Public URL: {public_url}")


if __name__ == "__main__":
    main()