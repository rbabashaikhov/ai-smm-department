
from __future__ import annotations

from pathlib import Path
from typing import Any

from langfuse import observe

from ai_smm.publishing.threads import ThreadsPublisher
from ai_smm.state import SMMState


PILOT_PROJECT_ID = "ai-catalog-consultant"

PILOT_MEDIA = [
    [
        "telegram-01-recommendation.jpg",
        "telegram-02-budget.jpg",
    ],
    [
        "telegram-03-comparison.jpg",
    ],
    [
        "telegram-04-tradeoff.jpg",
    ],
]


def prepare_publications(
    state: SMMState,
) -> list[dict[str, Any]]:
    draft = state["draft"]
    project_id = state["project_id"]
    knowledge = state["knowledge"]

    raw_publications = draft.get("publications", [])

    if not raw_publications:
        raise ValueError("No publications in draft.")

    assets = {
        Path(path).name: Path(path)
        for path in knowledge.get("assets", [])
    }

    prepared = []

    for index, publication in enumerate(
        raw_publications,
        start=1,
    ):
        items = publication.get("items", [])

        if not items:
            raise ValueError(
                f"Publication {index} has no text items."
            )

        texts = [
            item["text"].strip()
            for item in items
        ]

        if not all(texts):
            raise ValueError(
                f"Publication {index} contains empty text."
            )

        prepared_item = {
            "order": index,
            "title": publication["title"],
            "planned_format": publication["format"],
            "items": items,
            "text": "\n\n".join(texts),
            "images": [],
            "status": "ready",
        }

        prepared.append(prepared_item)

    if project_id == PILOT_PROJECT_ID:
        if len(prepared) != 3:
            raise ValueError(
                "Pilot project requires exactly 3 publications."
            )

        for index, publication in enumerate(prepared):
            filenames = PILOT_MEDIA[index]

            missing = [
                name
                for name in filenames
                if name not in assets
            ]

            if missing:
                raise FileNotFoundError(
                    f"Missing assets for publication {index + 1}: "
                    f"{missing}"
                )

            paths = [assets[name] for name in filenames]

            for path in paths:
                if not path.is_file():
                    raise FileNotFoundError(
                        f"Image file not found: {path}"
                    )

            publication["images"] = [
                {
                    "path": str(path),
                    "alt_text": (
                        "AI Catalog Consultant — "
                        + path.stem.replace("-", " ")
                    ),
                }
                for path in paths
            ]

            publication["format"] = (
                "carousel"
                if len(paths) > 1
                else "image"
            )

            if len(publication["text"]) > 500:
                raise ValueError(
                    f"Publication {index + 1} caption exceeds "
                    "500 characters. Revise the draft first."
                )

    else:
        for publication in prepared:
            publication["format"] = publication["planned_format"]

    return prepared


@observe(name="publisher", as_type="agent")
def publisher_node(state: SMMState) -> dict[str, Any]:
    approved = state.get("editor_approved", False)
    publish_live = state.get("publish_live", False)

    if not approved:
        return {
            "publication": {
                "status": "blocked",
                "platform": "threads",
                "reason": "Editor did not approve the content.",
                "publications": [],
            },
            "current_agent": "publisher",
        }

    try:
        publications = prepare_publications(state)

    except (ValueError, FileNotFoundError, KeyError) as exc:
        return {
            "publication": {
                "status": "blocked",
                "platform": "threads",
                "reason": str(exc),
                "publications": [],
            },
            "current_agent": "publisher",
        }

    if not publish_live:
        return {
            "publication": {
                "status": "dry_run",
                "platform": "threads",
                "publications": publications,
            },
            "current_agent": "publisher",
        }

    threads = ThreadsPublisher()
    published_results = []

    # Upload and validate every image before creating posts.
    # If storage fails, no Threads posts will be published.
    try:
        for publication in publications:
            uploaded_images = []

            for image in publication["images"]:
                public_url = threads.upload_image(
                    image["path"]
                )

                uploaded_images.append({
                    "url": public_url,
                    "alt_text": image["alt_text"],
                })

            publication["uploaded_images"] = uploaded_images

    except Exception as exc:
        return {
            "publication": {
                "status": "blocked",
                "platform": "threads",
                "reason": (
                    "Media preflight failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "publications": publications,
            },
            "current_agent": "publisher",
        }

    for publication in publications:
        publication_format = publication["format"]

        try:
            if publication_format == "carousel":
                result = threads.publish_carousel(
                    text=publication["text"],
                    images=publication["uploaded_images"],
                )

            elif publication_format == "image":
                image = publication["uploaded_images"][0]

                result = threads.create_image_post(
                    image_url=image["url"],
                    text=publication["text"],
                    alt_text=image["alt_text"],
                )

            elif publication_format == "single_post":
                result = threads.publish_text(
                    publication["text"]
                )

            elif publication_format == "thread":
                result = threads.publish_thread(
                    publication["items"]
                )

            else:
                raise ValueError(
                    "Unsupported publication format: "
                    f"{publication_format}"
                )

            status = "published"

        except Exception as exc:
            status = "failed"

            result = {
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

        published_results.append({
            **publication,
            "status": status,
            "result": result,
        })

        # Stop on the first failure to avoid attempting
        # subsequent posts when publication is unhealthy.
        if status != "published":
            break

    all_published = (
        len(published_results) == len(publications)
        and all(
            item["status"] == "published"
            for item in published_results
        )
    )

    return {
        "publication": {
            "status": (
                "published" if all_published else "partial"
            ),
            "platform": "threads",
            "publications": published_results,
        },
        "current_agent": "publisher",
    }
