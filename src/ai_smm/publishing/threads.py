from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from ai_smm.storage import StorageUploader


load_dotenv()


class ThreadsMediaFetchError(RuntimeError):
    """
    Meta accepted the request but could not download the media file.

    Graph API reports this as code 1 / error_subcode 2207052 with
    is_transient = false: Meta's media downloader could not fetch
    image_url, so retrying the same URL does not help.
    """

    def __init__(
        self,
        image_url: str,
        response_body: str,
        diagnosis: str,
    ) -> None:
        self.image_url = image_url
        self.response_body = response_body
        self.diagnosis = diagnosis

        super().__init__(
            "Threads could not download the media file "
            f"(error_subcode {ThreadsPublisher.MEDIA_FETCH_SUBCODE}, "
            "is_transient=false).\n"
            f"image_url: {image_url}\n"
            f"{diagnosis}\n"
            f"Meta response: {response_body}"
        )


class ThreadsPublisher:
    MEDIA_FETCH_SUBCODE = 2207052

    # Documented Threads image limits.
    # https://developers.facebook.com/docs/threads/posts
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MIN_IMAGE_WIDTH = 320
    MAX_IMAGE_WIDTH = 1440
    MAX_ASPECT_RATIO = 10.0
    SUPPORTED_IMAGE_FORMATS = ("JPEG", "PNG")

    def __init__(self) -> None:
        self.access_token = os.getenv("THREADS_ACCESS_TOKEN")
        self.base_url = os.getenv(
            "THREADS_API_BASE_URL",
            "https://graph.threads.net",
        )

        self.storage_prefix = os.getenv(
            "THREADS_STORAGE_PREFIX",
            "ai-smm/threads",
        ).strip("/")

        if not self.access_token:
            raise RuntimeError(
                "THREADS_ACCESS_TOKEN is not set."
            )

        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        self._storage_uploader: StorageUploader | None = None

    def _get_storage_uploader(self) -> StorageUploader:
        """
        Create StorageUploader only when an image upload is actually needed.

        Text-only Threads publishing therefore does not require SSH/storage
        configuration.
        """

        if self._storage_uploader is None:
            self._storage_uploader = StorageUploader()

        return self._storage_uploader

    def _diagnose_media_url(
        self,
        image_url: str,
    ) -> str:
        """
        Collect evidence about a media URL Meta refused to download.

        error_subcode 2207052 is reported with is_transient = false, so this
        is a one-shot diagnostic, not a retry: it tells us whether the URL is
        broken for everyone, or only unreachable from Meta's network.
        """

        lines: list[str] = []

        try:
            response = httpx.get(
                image_url,
                follow_redirects=True,
                timeout=30,
            )

            lines.append(
                f"Our own GET of the URL: HTTP {response.status_code}, "
                f"content-type={response.headers.get('content-type')!r}, "
                f"bytes={len(response.content)}, "
                f"redirects={len(response.history)}"
            )

            if response.status_code < 400 and response.content[:2] == b"\xff\xd8":
                lines.append(
                    "Magic bytes look like a valid JPEG."
                )

            if response.status_code < 400:
                lines.append(
                    "The URL serves the file correctly, so the file itself "
                    "is not the problem: Meta's media downloader could not "
                    "reach or complete a TLS connection to this host. "
                    "Serve the media from a host Meta can reach (for example "
                    "a CDN in front of the storage) and point "
                    "STORAGE_PUBLIC_BASE_URL at it."
                )

        except httpx.HTTPError as exc:
            lines.append(
                f"Our own GET of the URL failed: {exc!r}. "
                "Fix public hosting first."
            )

        return "\n".join(lines)

    def _create_media_container(
        self,
        *,
        image_url: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        """
        Create a Threads media container.

        Fails fast on error_subcode 2207052: Meta marks it is_transient=false,
        so retrying the same URL only hides the real cause.
        """

        response = httpx.post(
            f"{self.base_url}/me/threads",
            headers=self.headers,
            data=payload,
            timeout=30,
        )

        if response.status_code >= 400:
            print("\n=== THREADS API ERROR ===")
            print("status:", response.status_code)
            print("body:", response.text)

            subcode = None

            try:
                subcode = response.json().get("error", {}).get(
                    "error_subcode"
                )
            except Exception:
                subcode = None

            if subcode == self.MEDIA_FETCH_SUBCODE:
                raise ThreadsMediaFetchError(
                    image_url=image_url,
                    response_body=response.text,
                    diagnosis=self._diagnose_media_url(image_url),
                )

            response.raise_for_status()

        return response

    def validate_image_file(
        self,
        local_path: str | Path,
    ) -> dict[str, Any]:
        """
        Check a local image against the documented Threads image limits.

        Raises ValueError for a violation that Meta is known to reject,
        and prints a warning for soft limits Meta resizes around.
        """

        from PIL import Image

        local_file = Path(local_path).expanduser().resolve()

        size_bytes = local_file.stat().st_size

        with Image.open(local_file) as image:
            image_format = image.format
            width, height = image.size
            mode = image.mode
            progressive = bool(image.info.get("progressive"))

        info = {
            "path": str(local_file),
            "format": image_format,
            "width": width,
            "height": height,
            "mode": mode,
            "size_bytes": size_bytes,
            "progressive": progressive,
        }

        if image_format not in self.SUPPORTED_IMAGE_FORMATS:
            raise ValueError(
                f"Threads supports {self.SUPPORTED_IMAGE_FORMATS} images, "
                f"got {image_format} for {local_file}."
            )

        if size_bytes > self.MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image is {size_bytes} bytes, Threads limit is "
                f"{self.MAX_IMAGE_BYTES} bytes: {local_file}"
            )

        if width < self.MIN_IMAGE_WIDTH:
            raise ValueError(
                f"Image width {width}px is below the Threads minimum of "
                f"{self.MIN_IMAGE_WIDTH}px: {local_file}"
            )

        ratio = max(width, height) / min(width, height)

        if ratio > self.MAX_ASPECT_RATIO:
            raise ValueError(
                f"Image aspect ratio {ratio:.2f}:1 exceeds the Threads "
                f"limit of {self.MAX_ASPECT_RATIO:.0f}:1: {local_file}"
            )

        if width > self.MAX_IMAGE_WIDTH:
            print(
                f"WARNING: image width {width}px exceeds the documented "
                f"maximum of {self.MAX_IMAGE_WIDTH}px; Threads will "
                "downscale it."
            )

        return info

    def _build_storage_path(
        self,
        local_path: str | Path,
    ) -> str:
        """
        Build a unique public-storage path.

        Example:

            ai-smm/threads/20261007T151500Z-a12b34cd-photo.jpg

        A unique filename prevents Meta/CDN caches from reusing an older
        version of an image with the same URL.
        """

        local_file = Path(local_path)

        suffix = local_file.suffix.lower()

        if not suffix:
            raise ValueError(
                f"Image file must have an extension: {local_file}"
            )

        safe_stem = "".join(
            char
            if char.isalnum() or char in {"-", "_"}
            else "-"
            for char in local_file.stem
        ).strip("-_")

        if not safe_stem:
            safe_stem = "image"

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")

        unique_id = uuid4().hex[:8]

        filename = (
            f"{timestamp}-"
            f"{unique_id}-"
            f"{safe_stem}"
            f"{suffix}"
        )

        return f"{self.storage_prefix}/{filename}"

    def upload_image(
        self,
        local_path: str | Path,
    ) -> str:
        """
        Upload a local image to public storage and return its HTTPS URL.
        """

        local_file = Path(local_path).expanduser().resolve()

        if not local_file.exists():
            raise FileNotFoundError(
                f"Local image not found: {local_file}"
            )

        if not local_file.is_file():
            raise ValueError(
                f"Image path is not a file: {local_file}"
            )

        image_info = self.validate_image_file(
            local_file
        )

        print("\n=== IMAGE CHECK ===")
        print(image_info)

        remote_path = self._build_storage_path(
            local_file
        )

        uploader = self._get_storage_uploader()

        print("\n=== STORAGE UPLOAD ===")
        print("local:", local_file)
        print("remote:", remote_path)

        public_url = uploader.upload(
            local_path=local_file,
            remote_path=remote_path,
            verify_public_url=True,
        )

        print("public URL:", public_url)

        return public_url

    def publish_text(
        self,
        text: str,
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/me/threads",
            headers=self.headers,
            data={
                "media_type": "TEXT",
                "text": text,
                "auto_publish_text": "true",
            },
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    def create_reply(
        self,
        text: str,
        reply_to_id: str,
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/me/threads",
            headers=self.headers,
            data={
                "media_type": "TEXT",
                "text": text,
                "reply_to_id": reply_to_id,
            },
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    def publish_container(
        self,
        creation_id: str,
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/me/threads_publish",
            headers=self.headers,
            data={
                "creation_id": creation_id,
            },
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    def publish_thread(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not items:
            return []

        published: list[dict[str, Any]] = []

        first = self.publish_text(
            items[0]["text"]
        )

        published.append(first)

        previous_post_id = first.get("id")

        for item in items[1:]:
            if not previous_post_id:
                raise RuntimeError(
                    "Threads did not return post id."
                )

            container = self.create_reply(
                text=item["text"],
                reply_to_id=previous_post_id,
            )

            creation_id = container.get("id")

            if not creation_id:
                raise RuntimeError(
                    "Threads did not return creation id."
                )

            reply = self.publish_container(
                creation_id
            )

            published.append(reply)

            previous_post_id = reply.get("id")

        return published

    def create_image_post(
        self,
        image_url: str,
        text: str = "",
        alt_text: str = "",
    ) -> dict[str, Any]:
        """
        Publish an image that is already available by public HTTPS URL.
        """

        print("\n=== THREADS IMAGE POST ===")
        print("image_url:", image_url)

        response = self._create_media_container(
            image_url=image_url,
            payload={
                "media_type": "IMAGE",
                "image_url": image_url,
                "text": text,
                "alt_text": alt_text,
            },
        )

        container = response.json()

        creation_id = container.get("id")

        if not creation_id:
            raise RuntimeError(
                "Threads did not return image creation id."
            )

        print("creation_id:", creation_id)

        return self.publish_container(
            creation_id
        )

    def create_image_post_from_file(
        self,
        local_path: str | Path,
        text: str = "",
        alt_text: str = "",
    ) -> dict[str, Any]:
        """
        Upload a local image to public storage and publish it to Threads.
        """

        image_url = self.upload_image(
            local_path
        )

        return self.create_image_post(
            image_url=image_url,
            text=text,
            alt_text=alt_text,
        )

    def create_image_item(
        self,
        image_url: str,
        alt_text: str = "",
    ) -> dict[str, Any]:
        response = self._create_media_container(
            image_url=image_url,
            payload={
                "media_type": "IMAGE",
                "image_url": image_url,
                "alt_text": alt_text,
                "is_carousel_item": "true",
            },
        )

        return response.json()

    def create_carousel(
        self,
        text: str,
        children: list[str],
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/me/threads",
            headers=self.headers,
            params={
                "media_type": "CAROUSEL",
                "text": text,
                "children": ",".join(children),
            },
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    def publish_carousel(
        self,
        text: str,
        images: list[dict[str, str]],
    ) -> dict[str, Any]:
        """
        Publish a carousel from images that already have public URLs.

        Expected input:

            [
                {
                    "url": "https://...",
                    "alt_text": "..."
                },
                {
                    "url": "https://...",
                    "alt_text": "..."
                },
            ]
        """

        if len(images) < 2:
            raise ValueError(
                "Carousel requires at least 2 images."
            )

        child_ids: list[str] = []

        for index, image in enumerate(
            images,
            start=1,
        ):
            image_url = image["url"]

            print(
                f"\n=== CAROUSEL IMAGE {index}/{len(images)} ==="
            )
            print("image_url:", image_url)

            item = self.create_image_item(
                image_url=image_url,
                alt_text=image.get(
                    "alt_text",
                    "",
                ),
            )

            creation_id = item.get("id")

            if not creation_id:
                raise RuntimeError(
                    "Threads did not return image creation id."
                )

            print("creation_id:", creation_id)

            child_ids.append(
                creation_id
            )

        carousel = self.create_carousel(
            text=text,
            children=child_ids,
        )

        carousel_id = carousel.get("id")

        if not carousel_id:
            raise RuntimeError(
                "Threads did not return carousel creation id."
            )

        print("\n=== CAROUSEL CONTAINER ===")
        print("creation_id:", carousel_id)

        return self.publish_container(
            carousel_id
        )

    def publish_carousel_from_files(
        self,
        text: str,
        images: list[dict[str, str]],
    ) -> dict[str, Any]:
        """
        Upload local images to public storage and publish them as a carousel.

        Expected input:

            [
                {
                    "path": "./image-1.jpg",
                    "alt_text": "First image"
                },
                {
                    "path": "./image-2.jpg",
                    "alt_text": "Second image"
                },
            ]
        """

        if len(images) < 2:
            raise ValueError(
                "Carousel requires at least 2 images."
            )

        public_images: list[dict[str, str]] = []

        for index, image in enumerate(
            images,
            start=1,
        ):
            local_path = image.get("path")

            if not local_path:
                raise ValueError(
                    f"Carousel image {index} has no 'path'."
                )

            print(
                f"\n=== UPLOAD CAROUSEL IMAGE "
                f"{index}/{len(images)} ==="
            )

            public_url = self.upload_image(
                local_path
            )

            public_images.append(
                {
                    "url": public_url,
                    "alt_text": image.get(
                        "alt_text",
                        "",
                    ),
                }
            )

        return self.publish_carousel(
            text=text,
            images=public_images,
        )