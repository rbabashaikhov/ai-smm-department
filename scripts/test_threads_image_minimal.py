"""
Publish a minimal known-good JPEG (1080x1080, baseline, sRGB) to Threads.

The image is generated locally, uploaded with StorageUploader and published,
so the only variable under test is the storage host.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from pprint import pprint

from dotenv import load_dotenv

load_dotenv()

from PIL import Image

from ai_smm.publishing.threads import ThreadsPublisher


def build_minimal_jpeg(path: Path) -> Path:
    image = Image.new("RGB", (1080, 1080), (24, 96, 180))

    image.save(
        path,
        format="JPEG",
        quality=85,
        progressive=False,
        optimize=False,
    )

    return path


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="threads-minimal-"))

    local_path = build_minimal_jpeg(
        temp_dir / "minimal-1080x1080-baseline.jpg"
    )

    publisher = ThreadsPublisher()

    result = publisher.create_image_post_from_file(
        local_path=local_path,
        text="Minimal baseline JPEG test.",
        alt_text="Solid blue 1080x1080 baseline JPEG",
    )

    print("\n=== RESULT ===")
    pprint(result)


if __name__ == "__main__":
    main()
