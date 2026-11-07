"""Validated payloads accepted by the management API."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Tag = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FieldName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
FieldValue = Annotated[str, StringConstraints(max_length=65536)]


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=256)
    service: str = Field(default="", max_length=256)
    username: str = Field(default="", max_length=4096)
    password: str | None = Field(default=None, max_length=10000)
    description: str = Field(default="", max_length=2000)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=65536)
    fields: dict[FieldName, FieldValue] = Field(default_factory=dict, max_length=50)
    version: int | None = Field(default=None, ge=1)


class PasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class KeyInput(BaseModel):
    api_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512, pattern=r"^[^\r\n]+$")]


class QRInput(BaseModel):
    qrcode: str = Field(min_length=1, max_length=4096)
    verify_code: str | None = Field(default=None, max_length=64)


