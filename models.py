from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    String, Integer, Boolean, Text, DateTime, ForeignKey, JSON, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    pass


class User(Base):
    """Профіль студента або адміністратора вебчату."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(150))
    faculty: Mapped[Optional[str]] = mapped_column(String(100))
    course: Mapped[Optional[int]] = mapped_column(Integer)
    group_name: Mapped[Optional[str]] = mapped_column(String(50), index=True, default="ІПЗ-31")

    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_categories: Mapped[list] = mapped_column(JSON, default=list)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    messages: Mapped[List["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    favorites: Mapped[List["FavoriteSchedule"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class FavoriteSchedule(Base):
    """Збережені групи або викладачі студента."""
    __tablename__ = "favorite_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_type: Mapped[str] = mapped_column(String(20), default="group")  # group / teacher
    target_name: Mapped[str] = mapped_column(String(100))

    user: Mapped["User"] = relationship(back_populates="favorites")


class ChatMessage(Base):
    """Історія діалогу для підтримки контексту ШІ-консультацій."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="messages")


class KnowledgeChunk(Base):
    """Векторизовані фрагменти документів університету (RAG)."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_name: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[Optional[str]] = mapped_column(String(500))
    chunk_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(1536))
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Notification(Base):
    """Оголошення та сповіщення для студентів."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_group: Mapped[Optional[str]] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(50))  # schedule_changes / deadlines / general
    message_text: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScheduleCache(Base):
    """Кеш розкладу, отриманого із зовнішнього API університету."""
    __tablename__ = "schedule_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    schedule_data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )