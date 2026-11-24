"""POST /v1/feedback: what happened after a search. Joins on search_id; entities only, no caller identity.

"Ignored" is not posted: it is derived later (results shown, no outcome within the session).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .envelope import Strict

Outcome = Literal[
    "opened",          # caller looked at the entity (GET /entities, profile view)
    "saved",           # added to a list / watchlist
    "exported",
    "enriched",        # asked for more fields or a live refresh
    "contacted",       # outreach sent
    "replied",
    "meeting_booked",  # the real outcome
    "dismissed",       # explicit negative: this result was wrong or irrelevant
]


class FeedbackRequest(Strict):
    search_id: str = Field(description="From the search_id on a /search or /filter response.")
    outcome: Outcome
    entity_id: str | None = Field(None, description="The result acted on. Omit for search-level outcomes.")


class FeedbackData(BaseModel):
    recorded: bool
    rank: int | None = Field(None, description="Rank of entity_id in the original result set, if it was there.")
