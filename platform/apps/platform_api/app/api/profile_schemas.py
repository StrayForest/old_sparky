from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from apps.platform_api.app.api.schemas import (
    DeadlockDreamSlotBulkItemRequest,
    DeadlockDreamSlotResponse,
    DeadlockProfileResponse,
    MyProfileResponse,
)


class ProfileWorkspaceResponse(BaseModel):
    profile: MyProfileResponse
    deadlock_profile: DeadlockProfileResponse | None = None
    dream_slots: list[DeadlockDreamSlotResponse] = Field(default_factory=list, max_length=6)


class CaptainProfileUpdateRequest(BaseModel):
    captain_team_name: str = Field(default="", max_length=15)
    slots: list[DeadlockDreamSlotBulkItemRequest] = Field(default_factory=list, max_length=6)

    @field_validator("captain_team_name", mode="before")
    @classmethod
    def normalize_team_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CaptainProfileResponse(BaseModel):
    captain_team_name: str = Field(default="", max_length=15)
    dream_slots: list[DeadlockDreamSlotResponse] = Field(default_factory=list, max_length=6)
