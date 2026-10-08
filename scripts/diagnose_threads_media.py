"""
Diagnose Threads image publishing end to end.

Changes one variable at a time:

1. control URL  — a known-good JPEG hosted outside our storage.
   Proves whether the token, the request and the IMAGE container flow work.

2. our storage  — the same bytes served from STORAGE_PUBLIC_BASE_URL.
   Proves whether Meta's media downloader can reach our host.

Containers are created but NOT published, so the account stays clean.

usage:
    uv run python scripts/diagnose_threads_media.py [--image path.jpg]
"""

from __future__ import annotations

import argparse

import httpx
from dotenv import load_dotenv

load_dotenv()

from ai_smm.publishing.threads import (
    ThreadsMediaFetchError,
    ThreadsPublisher,
)

CONTROL_IMAGE_URL = "https://www.gstatic.com/webp/gallery/1.jpg"


def check_public_url(url: str) -> None:
    print(f"\n--- HTTP check: {url}")

    response = httpx.get(url, follow_redirects=True, timeout=30)

    print("status:", response.status_code)
    print("redirects:", len(response.history))

    for header in (
        "content-type",
        "content-length",
        "content-encoding",
        "content-disposition",
        "cache-control",
        "accept-ranges",
        "x-robots-tag",
    ):
        if header in response.headers:
            print(f"{header}: {response.headers[header]}")

    print("magic bytes:", response.content[:4].hex())


def try_container(
    publisher: ThreadsPublisher,
    image_url: str,
) -> bool:
    print(f"\n--- container create: {image_url}")

    try:
        response = publisher._create_media_container(
            image_url=image_url,
            payload={
                "media_type": "IMAGE",
                "image_url": image_url,
                "text": "",
                "alt_text": "diagnostics",
            },
        )

    except ThreadsMediaFetchError as exc:
        print("FAILED: Meta could not download the media.")
        print(exc)
        return False

    except httpx.HTTPStatusError as exc:
        print("FAILED with a different Graph API error:")
        print(exc.response.text)
        return False

    print("OK, creation_id:", response.json().get("id"))
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        help=(
            "Local image to upload to our storage and test. "
            "Defaults to downloading the control image."
        ),
    )
    args = parser.parse_args()

    publisher = ThreadsPublisher()

    print("=== STEP 1: control URL (external host) ===")
    check_public_url(CONTROL_IMAGE_URL)
    control_ok = try_container(publisher, CONTROL_IMAGE_URL)

    print("\n=== STEP 2: same flow via our public storage ===")

    if args.image:
        storage_url = publisher.upload_image(args.image)
    else:
        content = httpx.get(
            CONTROL_IMAGE_URL,
            follow_redirects=True,
            timeout=30,
        ).content

        uploader = publisher._get_storage_uploader()

        storage_url = uploader.upload_bytes(
            content=content,
            remote_path=(
                f"{publisher.storage_prefix}/diagnostics-control.jpg"
            ),
        )

    check_public_url(storage_url)
    storage_ok = try_container(publisher, storage_url)

    print("\n=== VERDICT ===")
    print("control URL container:", "OK" if control_ok else "FAILED")
    print("our storage container:", "OK" if storage_ok else "FAILED")

    if control_ok and not storage_ok:
        print(
            "\nThe Threads API, the token, the request and the image are "
            "fine. Meta's media downloader cannot fetch from our storage "
            "host. This is a network-reachability problem between Meta's "
            "crawler network and the storage server, not a code problem."
        )
    elif not control_ok:
        print(
            "\nEven the control URL failed: investigate the token, the "
            "permissions or the Threads API itself."
        )
    else:
        print("\nBoth containers were created: image publishing works.")


if __name__ == "__main__":
    main()
