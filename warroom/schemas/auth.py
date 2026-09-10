"""Register / login / token / current-user payloads (SPEC 4). TODO (Phase 2)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

LowercaseEmail = Annotated[EmailStr, AfterValidator(str.lower)]


class RegisterIn(BaseModel):
    email: LowercaseEmail

    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: LowercaseEmail
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    created_at: datetime
