"""Board share payloads — share by email, permission read|edit (SPEC 4)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr

from warroom.models import SharePermission

LowercaseEmail = Annotated[EmailStr, AfterValidator(str.lower)]


class ShareCreate(BaseModel):
    email: LowercaseEmail
    permission: SharePermission


class ShareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board_id: uuid.UUID
    shared_with_user_id: uuid.UUID
    permission: SharePermission
    created_at: datetime
