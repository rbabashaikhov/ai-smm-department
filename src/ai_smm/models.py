from pydantic import BaseModel, Field


class PublicationItem(BaseModel):
    order: int
    text: str
    media: list[str] = Field(default_factory=list)


class PublicationPlan(BaseModel):
    format: str
    title: str
    items: list[PublicationItem]