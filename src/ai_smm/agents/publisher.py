from typing import Any

from httpx import HTTPStatusError
from langfuse import observe

from ai_smm.publishing.threads import ThreadsPublisher
from ai_smm.state import SMMState


@observe(name="publisher", as_type="agent")
def publisher_node(state: SMMState) -> dict[str, Any]:
    draft = state["draft"]
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

    publications = []

    for pub_index, publication in enumerate(
        draft["publications"],
        start=1,
    ):
        publications.append(
            {
                "order": pub_index,
                "title": publication["title"],
                "format": publication["format"],
                "items": publication["items"],
                "status": "ready",
            }
        )

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

    for publication in publications:
        publication_format = publication["format"]

        try:
            if publication_format == "single_post":
                result = threads.publish_text(
                    publication["items"][0]["text"]
                )

                status = "published"

            elif publication_format == "thread":
                result = threads.publish_thread(
                    publication["items"]
                )

                status = "published"

            elif publication_format == "carousel":
                result = {
                    "reason": (
                        "Carousel requires media URLs. "
                        "Automatic media publishing is currently "
                        "affected by Threads media-fetch failures."
                    )
                }

                status = "media_pending"

            else:
                result = {
                    "reason": (
                        f"Unsupported publication format: "
                        f"{publication_format}"
                    )
                }

                status = "unsupported_format"

        except HTTPStatusError as exc:
            response = exc.response

            error_payload = {}

            try:
                error_payload = response.json()
            except Exception:
                pass

            error = error_payload.get("error", {})
            subcode = error.get("error_subcode")

            if subcode == ThreadsPublisher.MEDIA_FETCH_SUBCODE:
                status = "media_fetch_failed"

                result = {
                    "http_status": response.status_code,
                    "error_subcode": subcode,
                    "message": error.get("message"),
                    "user_message": error.get("error_user_msg"),
                }

            else:
                status = "api_error"

                result = {
                    "http_status": response.status_code,
                    "body": response.text,
                }

        except Exception as exc:
            status = "failed"

            result = {
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

        published_results.append(
            {
                **publication,
                "status": status,
                "result": result,
            }
        )

    final_status = "published"

    if any(
        item["status"] != "published"
        for item in published_results
    ):
        final_status = "partial"

    return {
        "publication": {
            "status": final_status,
            "platform": "threads",
            "publications": published_results,
        },
        "current_agent": "publisher",
    }