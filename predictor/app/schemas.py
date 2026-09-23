from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["facebook", "instagram"]


class PredictRequest(BaseModel):
    platform: Platform
    content_type: str | None = Field(
        default=None,
        description="e.g. 'video', 'reel', 'image', 'carousel'. Omit if undecided yet.",
    )
    caption: str = Field(default="", max_length=5000)
    planned_posted_at: datetime = Field(
        description="ISO 8601 timestamp for when the post is planned to go live."
    )


class Reason(BaseModel):
    feature: str
    contribution: float


class PredictResponse(BaseModel):
    label: Literal["likely_below_typical", "likely_at_or_above_typical"]
    probability: float
    threshold: float
    model_version: str
    top_reasons: list[Reason]
