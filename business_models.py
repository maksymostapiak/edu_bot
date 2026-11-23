import io
from datetime import date, datetime, timedelta
from typing import List, Optional
import httpx
from pypdf import PdfReader
from sqlalchemy.orm import Session

from models import (
    User,
    FavoriteSchedule,
    ChatMessage,
    KnowledgeChunk,
    Notification,
    ScheduleCache,
)


# =====================================================================
# 1. МОДЕЛЬ КОРИСТУВАЧА (Профіль, вибране, налаштування)
# =====================================================================
class UserModel:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, viber_id: str, full_name: Optional[str] = None) -> User:
        user = self.db.query(User).filter_by(viber_id=viber_id).first()
        if not user:
            user = User(
                viber_id=viber_id,
                full_name=full_name,
                group_name="ІПЗ-31",
                push_enabled=True,
                notification_categories=["schedule_changes", "deadlines"],
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)
        return user

    def update_group(self, user: User, new_group: str) -> User:
        user.group_name = new_group.strip()
        self.db.commit()
        self.db.refresh(user)
        return user

    def toggle_push_notifications(self, user: User) -> bool:
        user.push_enabled = not user.push_enabled
        self.db.commit()
        return user.push_enabled

    def update_categories(self, user: User, categories: List[str]) -> User:
        user.notification_categories = categories
        self.db.commit()
        self.db.refresh(user)
        return user

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
# 2. МОДЕЛЬ РОЗКЛАДУ (Отримання даних, кешування, джерело API)
# =====================================================================
class ScheduleModel:
    # Налаштування джерела (оновлюються з адмін-панелі)
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
        """Повертає сирі структуровані дані розкладу на конкретний день."""
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

            data = {
                "lessons": [
                    f"1. 08:30 — Програмна інженерія ({target})",
                    "2. 10:15 — Бази даних",
                ]
            }

        if cached:
            cached.schedule_data = data
        else:
            self.db.add(ScheduleCache(cache_key=cache_key, schedule_data=data))
        self.db.commit()

        return {"target": target, "date": target_date, "lessons": data.get("lessons", [])}

    async def get_weekly_schedule(self, target: str, start_date: Optional[date] = None) -> List[dict]:
        """Повертає розклад на 5 навчальних днів."""
        base_date = start_date or date.today()
        return [
            await self.get_daily_schedule(target, base_date + timedelta(days=i))
            for i in range(5)
        ]


# =====================================================================
# 3. МОДЕЛЬ ШІ-КОНСУЛЬТАЦІЇ (RAG-пошук, історія, індексація PDF)
# =====================================================================
class AiConsultationModel:
    def __init__(self, db: Session):
        self.db = db

    async def create_embedding(self, text: str) -> List[float]:
        """
        Перетворення тексту у вектор.
        Тут підключається реальний виклик OpenAI / Gemini Embeddings API.
        """
        return [0.0] * 1536

    async def generate_llm_response(self, prompt: str, history: List[ChatMessage]) -> str:
        """
        Генерація відповіді через зовнішню LLM.
        Тут підключається реальний виклик OpenAI / Gemini API.
        """
        return (
            f"Відповідь на основі бази знань університету.\n"
            f"(Враховано {len(history)} попередніх повідомлень контексту)."
        )

    async def search_knowledge_base(self, query: str, top_k: int = 3) -> List[KnowledgeChunk]:
        """Семантичний пошук найближчих фрагментів у PostgreSQL (pgvector)."""
        query_vector = await self.create_embedding(query)
        return (
            self.db.query(KnowledgeChunk)
            .order_by(KnowledgeChunk.embedding.cosine_distance(query_vector))
            .limit(top_k)
            .all()
        )

    def get_recent_history(self, user_id: int, limit: int = 5) -> List[ChatMessage]:
        messages = (
            self.db.query(ChatMessage)
            .filter_by(user_id=user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        messages.reverse()
        return messages

    def clear_context(self, user_id: int) -> None:
        self.db.query(ChatMessage).filter_by(user_id=user_id).delete()
        self.db.commit()

    async def process_query(self, user: User, query: str) -> dict:
        """Повний RAG-цикл: пошук контексту -> LLM -> збереження в історію."""
        chunks = await self.search_knowledge_base(query, top_k=3)
        context_text = "\n---\n".join(c.chunk_text for c in chunks)
        sources = list({c.source_url for c in chunks if c.source_url})

        history = self.get_recent_history(user.id, limit=5)
        prompt = f"Контекст з документів ЗВО:\n{context_text}\n\nЗапитання студента: {query}"

        answer = await self.generate_llm_response(prompt, history)

        # Зберігаємо запит та відповідь для підтримки контексту бесіди
        self.db.add(ChatMessage(user_id=user.id, role="user", content=query))
        self.db.add(ChatMessage(user_id=user.id, role="assistant", content=answer))
        self.db.commit()

        return {"answer": answer, "sources": sources}

    async def upload_document(
        self, filename: str, content_bytes: bytes, source_url: Optional[str] = None, chunk_size: int = 500
    ) -> int:
        """Парсинг PDF/TXT, розбиття на чанки та збереження векторів у БД."""
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
# 4. МОДЕЛЬ СПОВІЩЕНЬ (Створення подій та вибірка отримувачів)
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
            is_sent=False,
        )
        self.db.add(notif)
        self.db.commit()
        self.db.refresh(notif)
        return notif

    def get_recipients(self, notification: Notification, course: str = "all") -> List[User]:
        """Вибирає студентів, яким має надійти конкретне сповіщення."""
        query = self.db.query(User).filter(User.push_enabled.is_(True))

        if notification.target_group:
            query = query.filter(User.group_name == notification.target_group)
        elif course != "all":
            query = query.filter(User.course == int(course))

        users = query.all()
        return [
            u for u in users
            if not u.notification_categories or notification.category in u.notification_categories
        ]

    def mark_as_sent(self, notification: Notification) -> None:
        notification.is_sent = True
        self.db.commit()