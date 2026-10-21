"""A project's knowledge documents, read-only (SMM-034A).

Every member may read them, like every other project read. There is no
write route: the knowledge base is a host directory that production
mounts read-only into the worker only, and the Control Center has no
writable storage for it (docs/architecture/smm-034a-project-knowledge-base.md).
"""
from __future__ import annotations

from fastapi import APIRouter

from ai_smm.api.dependencies import AppSettings, ViewerProject
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.schemas import (
    KnowledgeDocumentContentOut,
    KnowledgeDocumentOut,
    KnowledgeOut,
)
from ai_smm.knowledge.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentInfo,
    DocumentNotFound,
    DocumentTooLarge,
    KnowledgeUnavailable,
    list_documents,
    read_document,
)


router = APIRouter(prefix="/projects", tags=["knowledge"])


@router.get(
    "/{project_id}/knowledge",
    response_model=KnowledgeOut,
    summary="The project's knowledge documents (viewer and above)",
)
def get_knowledge(context: ViewerProject, settings: AppSettings) -> KnowledgeOut:
    project_id = context.project.id

    try:
        documents = list_documents(settings.knowledge_root, project_id)
    except KnowledgeUnavailable:
        return KnowledgeOut(
            project_id=project_id, available=False, writable=False, documents=[]
        )

    return KnowledgeOut(
        project_id=project_id,
        available=True,
        writable=False,
        documents=[_document_out(info) for info in documents],
    )


@router.get(
    "/{project_id}/knowledge/{document_name}",
    response_model=KnowledgeDocumentContentOut,
    summary="One knowledge document with its text (viewer and above)",
)
def get_knowledge_document(
    document_name: str, context: ViewerProject, settings: AppSettings
) -> KnowledgeDocumentContentOut:
    try:
        document = read_document(
            settings.knowledge_root, context.project.id, document_name
        )
    except DocumentNotFound as exc:
        raise not_found("Knowledge document not found.") from exc
    except DocumentTooLarge as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"Knowledge document exceeds {MAX_DOCUMENT_BYTES} bytes."
            ),
        ) from exc

    return KnowledgeDocumentContentOut(
        **_document_out(document.info).model_dump(),
        sha256=document.sha256,
        content=document.content,
    )


def _document_out(info: DocumentInfo) -> KnowledgeDocumentOut:
    return KnowledgeDocumentOut(
        name=info.name,
        kind=info.kind,
        size_bytes=info.size_bytes,
        updated_at=info.updated_at,
    )
