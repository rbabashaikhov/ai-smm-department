"""Publish one approved Threads post from a durable JSON queue.

Run from anywhere inside ai-smm-department after placing the JSON in
 data/publications/ai-catalog-consultant.json.

Examples:
    uv run python scripts/publish_saved.py --post 1
    uv run python scripts/publish_saved.py --post 1 --live --confirm-reviewed
    uv run python scripts/publish_saved.py --due --live --confirm-reviewed

Never automatically retry an ambiguous Threads publish. Inspect the account
and reconcile needs_review entries manually.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "data/publications/ai-catalog-consultant.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@contextmanager
def exclusive_lock(queue: Path):
    lock_path = queue.with_suffix(queue.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def parse_schedule(value: str | None) -> datetime | None:
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("scheduled_at must contain a timezone offset, e.g. +03:00")
    return result.astimezone(timezone.utc)


def find_posts(document: dict[str, Any], *, post: int | None, due: bool) -> list[dict[str, Any]]:
    publications = document.get("publications")
    if not isinstance(publications, list):
        raise ValueError("JSON queue is missing publications list")
    if post is not None:
        selected = [p for p in publications if p.get("order") == post]
        if len(selected) != 1:
            raise ValueError(f"Post {post} must occur exactly once; found {len(selected)}")
        return selected
    if due:
        current = datetime.now(timezone.utc)
        return [p for p in publications if p.get("status") == "approved" and
                (scheduled := parse_schedule(p.get("scheduled_at"))) is not None and scheduled <= current]
    raise ValueError("Specify --post NUMBER or --due")


def validate_post(item: dict[str, Any], *, require_due: bool) -> list[dict[str, str]]:
    if item.get("status") != "approved":
        raise ValueError(f"Post {item.get('order')} status={item.get('status')!r}; expected 'approved'")
    if item.get("threads_post_id") or item.get("published_at"):
        raise ValueError("Post has existing publish metadata; refusing duplicate")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 450:
        raise ValueError("Post text must be nonempty and <=450 characters")
    if "samsung" in text.lower() or "самсунг" in text.lower():
        raise ValueError("Historical brand found in public caption")
    scheduled = parse_schedule(item.get("scheduled_at"))
    if require_due and (scheduled is None or scheduled > datetime.now(timezone.utc)):
        raise ValueError("Post has not reached scheduled_at")
    images = item.get("images")
    if not isinstance(images, list):
        raise ValueError("Post images must be a list")
    prepared = []
    for image in images:
        relative_path = Path(image["path"])
        if relative_path.is_absolute():
            raise ValueError("Image paths must be relative to project root")
        real_path = (ROOT / relative_path).resolve()
        if not real_path.is_relative_to(ROOT) or not real_path.is_file():
            raise ValueError(f"Missing/unsafe image: {image['path']}")
        prepared.append({"path": str(real_path), "alt_text": image.get("alt_text", "")})
    fmt = item.get("format")
    if (fmt == "image" and len(prepared) != 1) or (fmt == "carousel" and not 2 <= len(prepared) <= 20):
        raise ValueError(f"Format {fmt} incompatible with {len(prepared)} images")
    if fmt == "text" and prepared:
        raise ValueError("Text-only post cannot contain images")
    if fmt not in ("image", "carousel", "text"):
        raise ValueError(f"Unsupported format: {fmt}")
    return prepared


def publish_one(publisher: Any, item: dict[str, Any], images: list[dict[str, str]]) -> dict[str, Any]:
    fmt = item["format"]
    if fmt == "carousel":
        result = publisher.publish_carousel_from_files(text=item["text"], images=images)
    elif fmt == "image":
        image = images[0]
        result = publisher.create_image_post_from_file(
            local_path=image["path"], text=item["text"], alt_text=image["alt_text"]
        )
    else:
        result = publisher.publish_text(item["text"])
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("Threads response has no post id; manual reconciliation required")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--post", type=int, help="Publish one post by order")
    selector.add_argument("--due", action="store_true", help="Publish approved posts past scheduled_at")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--live", action="store_true", help="Actually contact Threads; default is preview")
    parser.add_argument("--confirm-reviewed", action="store_true", help="Confirm human review of the saved texts and images")
    args = parser.parse_args()
    queue = args.queue.expanduser().resolve()
    if not queue.is_file():
        parser.error(f"Queue not found: {queue}")
    if args.live and not args.confirm_reviewed:
        parser.error("For a live request, pass --confirm-reviewed after checking text and screenshots")
    load_dotenv(ROOT / ".env")

    try:
        with exclusive_lock(queue):
            document = json.loads(queue.read_text(encoding="utf-8"))
            if document.get("approval", {}).get("approved") is not True:
                raise ValueError("Queue has not been approved by Editor")
            selected = find_posts(document, post=args.post, due=args.due)
            if not selected:
                print("No scheduled posts are due.")
                return 0
            publisher = None
            for item in selected:
                images = validate_post(item, require_due=args.due)
                print(f"Post {item['order']} | {item.get('title')} | {item['format']} | chars={len(item['text'])} | images={len(images)}", flush=True)
                print(item["text"], flush=True)
                if not args.live:
                    print("DRY RUN — nothing sent.\n", flush=True)
                    continue
                # Write a recoverable marker BEFORE contacting any external system.
                item["status"] = "publishing"
                item["attempt_started_at"] = now_utc()
                save_json(queue, document)
                try:
                    if publisher is None:
                        from ai_smm.publishing.threads import ThreadsPublisher
                        publisher = ThreadsPublisher()
                    result = publish_one(publisher, item, images)
                    item["status"] = "published"
                    item["threads_post_id"] = str(result["id"])
                    item["published_at"] = now_utc()
                    item.pop("last_error", None)
                    save_json(queue, document)
                    print(f"PUBLISHED post {item['order']}: Threads id {item['threads_post_id']}", flush=True)
                except BaseException as exc:
                    item["status"] = "needs_review"
                    item["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                    save_json(queue, document)
                    print(f"STOP: Post {item['order']} status=needs_review. Check Threads before any retry.", file=sys.stderr, flush=True)
                    raise
            return 0
    except BlockingIOError:
        print("Queue is locked by another publishing process.", file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
