import io
from datetime import date, datetime, timedelta
from typing import List, Optional
import httpx
from pypdf import PdfReader
from sqlalchemy.orm import Session
import os
from google import genai
from google.genai import types

from models import (
    User,
    FavoriteSchedule,
    ChatMessage,
    KnowledgeChunk,
    Notification,
    ScheduleCache,
)


# =====================================================================
# 1. МОДЕЛЬ КОРИСТУВАЧА
# =====================================================================
class UserModel:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, session_id: str, full_name: Optional[str] = None) -> User:
        user = self.db.query(User).filter_by(session_id=session_id).first()
        if not user:
            user = User(
                session_id=session_id,
                full_name=full_name or "Студент",
                group_name="ІПЗ-31",
                notifications_enabled=True,
                notification_categories=["schedule_changes", "deadlines", "general"],
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)
        return user

    def toggle_notifications(self, user: User) -> bool:
        user.notifications_enabled = not user.notifications_enabled
        self.db.commit()
        return user.notifications_enabled

    def add_favorite_schedule(self, user: User, target_name: str, target_type: str = "group") -> FavoriteSchedule:
        existing = (
            self.db.query(FavoriteSchedule)
            .filter_by(user_id=user.id, target_name=target_name)
            .first()
        )
        if existing:
            return existing

        fav = FavoriteSchedule(
            user_id=user.id,
            target_type=target_type,
            target_name=target_name,
        )
        self.db.add(fav)
        self.db.commit()
        self.db.refresh(fav)
        return fav

    def get_favorites(self, user: User) -> List[FavoriteSchedule]:
        return self.db.query(FavoriteSchedule).filter_by(user_id=user.id).all()


# =====================================================================
# 2. МОДЕЛЬ РОЗКЛАДУ (Зовнішнє API + Кешування)
# =====================================================================
class ScheduleModel:
    api_endpoint: str = "https://api.university.edu.ua/v1/schedule"
    cache_timeout_minutes: int = 30

    def __init__(self, db: Session):
        self.db = db

    @classmethod
    def update_config(cls, db: Session, api_endpoint: str, cache_timeout: int) -> dict:
        cls.api_endpoint = api_endpoint
        cls.cache_timeout_minutes = cache_timeout
        db.query(ScheduleCache).delete()
        db.commit()
        return {
            "api_endpoint": cls.api_endpoint,
            "cache_timeout_minutes": cls.cache_timeout_minutes,
        }

    async def get_daily_schedule(self, target: str, target_date: date) -> dict:
        cache_key = f"{target}_{target_date.isoformat()}"
        cached = self.db.query(ScheduleCache).filter_by(cache_key=cache_key).first()

        if cached and cached.updated_at:
            age = datetime.utcnow() - cached.updated_at
            if age < timedelta(minutes=self.cache_timeout_minutes):
                return {"target": target, "date": target_date, "lessons": cached.schedule_data.get("lessons", [])}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    self.api_endpoint,
                    params={"target": target, "date": target_date.isoformat()},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            # Фолбек-дані, якщо зовнішній сервер ЗВО недоступний
            data = {
                "lessons": [
                    f"1. 08:30 — Програмна інженерія ({target})",
                    "2. 10:15 — Бази даних та знань",
                    "3. 12:10 — Вебтехнології",
                ]
            }

        if cached:
            cached.schedule_data = data
        else:
            self.db.add(ScheduleCache(cache_key=cache_key, schedule_data=data))
        self.db.commit()

        return {"target": target, "date": target_date, "lessons": data.get("lessons", [])}

    async def get_weekly_schedule(self, target: str, start_date: Optional[date] = None) -> List[dict]:
        base_date = start_date or date.today()
        return [
            await self.get_daily_schedule(target, base_date + timedelta(days=i))
            for i in range(5)
        ]


# =====================================================================
# 3. МОДЕЛЬ ШІ-КОНСУЛЬТАЦІЇ (RAG + pgvector + Історія)
# =====================================================================
class AiConsultationModel:
    def __init__(self, db: Session):
        self.db = db
        api_key = os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.chat_model = "gemini-3.8-flash"
        self.embed_model = "gemini-embedding-001"

    async def create_embedding(self, text: str) -> List[float]:
        """Генерація реального вектора (1536 вимірів) через Gemini Embeddings API."""
        if not self.client:
            return [0.0] * 1536

        result = self.client.models.embed_content(
            model=self.embed_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=1536),
        )
        return result.embeddings[0].values

    async def generate_llm_response(
        self, query: str, context_text: str, history: List[ChatMessage]
    ) -> str:
        """Генерація відповіді через Gemini з урахуванням бази знань (RAG) та історії чату."""
        if not self.client:
            return "⚠️ GEMINI_API_KEY не налаштовано. Додайте ключ у файл .env."

        system_instruction = (
            "Ти — корисний освітній чат-бот університету. "
            "Відповідай українською мовою, чітко та по суті. "
            "Якщо в наданому контексті з бази знань є відповідь — спирайся насамперед на нього. "
            "Якщо контекст порожній або не містить відповіді, чесно скажи про це і дай загальну пораду."
        )

        # Формуємо історію діалогу для контексту
        contents = []
        for msg in history:
            role = "model" if msg.role == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))

        # Додаємо поточний запит разом із знайденим RAG-контекстом
        user_prompt = (
            f"Контекст із документів університету:\n{context_text or 'Документів не знайдено.'}\n\n"
            f"Запитання студента: {query}"
        )
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]))

        try:
            response = self.client.models.generate_content(
                model=self.chat_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.3,
                ),
            )
            return response.text or "Не вдалося згенерувати відповідь."
        except Exception as e:
            return f"⚠️ Помилка звернення до Gemini API: {e}"

    async def search_knowledge_base(self, query: str, top_k: int = 3) -> List[KnowledgeChunk]:
        query_vector = await self.create_embedding(query)
        return (
            self.db.query(KnowledgeChunk)
            .order_by(KnowledgeChunk.embedding.cosine_distance(query_vector))
            .limit(top_k)
            .all()
        )

    def get_recent_history(self, user_id: int, limit: int = 20) -> List[ChatMessage]:
        messages = (
            self.db.query(ChatMessage)
            .filter_by(user_id=user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        messages.reverse()
        return messages

    async def process_query(self, user: User, query: str) -> dict:
        chunks = await self.search_knowledge_base(query, top_k=3)
        context_text = "\n---\n".join(c.chunk_text for c in chunks)
        sources = list({c.source_url for c in chunks if c.source_url})

        history = self.get_recent_history(user.id, limit=6)
        answer = await self.generate_llm_response(query, context_text, history)

        self.db.add(ChatMessage(user_id=user.id, role="user", content=query))
        self.db.add(ChatMessage(user_id=user.id, role="assistant", content=answer))
        self.db.commit()

        return {"answer": answer, "sources": sources}

    async def upload_document(
        self, filename: str, content_bytes: bytes, source_url: Optional[str] = None, chunk_size: int = 500
    ) -> int:
        if filename.lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(content_bytes))
            full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif filename.lower().endswith(".txt"):
            full_text = content_bytes.decode("utf-8", errors="ignore")
        else:
            raise ValueError("Підтримуються лише формати PDF та TXT")

        chunks = [
            full_text[i : i + chunk_size].strip()
            for i in range(0, len(full_text), chunk_size)
            if full_text[i : i + chunk_size].strip()
        ]

        for text_part in chunks:
            vector = await self.create_embedding(text_part)
            self.db.add(
                KnowledgeChunk(
                    document_name=filename,
                    source_url=source_url,
                    chunk_text=text_part,
                    embedding=vector,
                )
            )

        self.db.commit()
        return len(chunks)
# =====================================================================
# 4. МОДЕЛЬ СПОВІЩЕНЬ
# =====================================================================
class NotificationModel:
    def __init__(self, db: Session):
        self.db = db

    def create_notification(
        self, message_text: str, category: str, target_group: Optional[str] = None
    ) -> Notification:
        priority = 2 if category == "schedule_changes" else 1
        notif = Notification(
            target_group=target_group,
            category=category,
            message_text=message_text,
            priority=priority,
            is_sent=True,
        )
        self.db.add(notif)
        self.db.commit()
        self.db.refresh(notif)
        return notif

    def get_user_notifications(self, user: User, limit: int = 5) -> List[Notification]:
        if not user.notifications_enabled:
            return []
        query = self.db.query(Notification).filter(
            (Notification.target_group.is_(None)) | (Notification.target_group == user.group_name)
        )
        return query.order_by(Notification.created_at.desc()).limit(limit).all()