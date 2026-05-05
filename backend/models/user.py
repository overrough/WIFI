import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    hashed_api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    active_mode: Mapped[str] = mapped_column(String, default="work")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profile: Mapped[list["UserProfile"]] = relationship(  # type: ignore
        back_populates="user", cascade="all, delete-orphan"
    )
    conversations: Mapped[list] = relationship(
        "Conversation", back_populates="user", cascade="all, delete-orphan"
    )
    memories: Mapped[list] = relationship(
        "Memory", back_populates="user", cascade="all, delete-orphan"
    )
    tasks: Mapped[list] = relationship(
        "Task", back_populates="user", cascade="all, delete-orphan"
    )

    def profile_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.profile}


class UserProfile(Base):
    __tablename__ = "user_profile"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String, default="explicit")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="profile")
