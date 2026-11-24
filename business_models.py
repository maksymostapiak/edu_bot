import io
import asyncio
from datetime import date, datetime, timedelta
from typing import List, Optional
import httpx
from pypdf import PdfReader
from sqlalchemy.orm import Session
import os
from google import genai
from google.genai import types
import re
from bs4 import BeautifulSoup
import json
import re
from sqlalchemy import func

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

    def update_group(self, user: User, group_name: str) -> User:
        cleaned = " ".join((group_name or "").strip().split())
        if not cleaned:
            raise ValueError("Назва групи не може бути порожньою")
        user.group_name = cleaned
        self.db.commit()
        self.db.refresh(user)
        return user

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
    api_endpoint: str = "https://asu.pnu.edu.ua/static/groups/1002/0-6326/index.js"
    groups_list_url: str = "https://asu.pnu.edu.ua/data/groups-list.js"
    cache_timeout_minutes: int = 30
    _groups_cache: list[dict] = []
    _groups_fetched_at: Optional[datetime] = None

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _js_array_to_json(js_text: str, var_name: str) -> list[dict]:
        """Витягує масив із JS-змінної (PNUgroups або PNUschedule) і парсить як JSON."""
        pattern = rf"(?:const|let|var)\s+{re.escape(var_name)}\s*=\s*(\[\s*.*?\s*\])\s*;"
        match = re.search(pattern, js_text, re.DOTALL)
        if not match:
            raise ValueError(f"Не знайдено масив {var_name}")

        raw_array = match.group(1)
        tokens = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', raw_array)
        cleaned_parts = []

        for tok in tokens:
            if not tok:
                continue
            if tok.startswith('"') and tok.endswith('"'):
                cleaned_parts.append(tok)
            elif tok.startswith("'") and tok.endswith("'"):
                inner = tok[1:-1].replace('"', '\\"')
                cleaned_parts.append(f'"{inner}"')
            else:
                part = re.sub(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', tok)
                part = re.sub(r",\s*([\]}])", r"\1", part)
                cleaned_parts.append(part)

        return json.loads("".join(cleaned_parts))

    @classmethod
    def _build_group_url(cls, faculty_id: int | str, group_id: int | str) -> str:
        folder = f"000000{group_id}"[-6:]
        return f"https://asu.pnu.edu.ua/static/groups/{faculty_id}/{folder}/index.js"

    async def _get_all_groups(self) -> list[dict]:
        now = datetime.utcnow()
        if (
            self._groups_cache
            and self._groups_fetched_at
            and (now - self._groups_fetched_at) < timedelta(minutes=max(self.cache_timeout_minutes, 60))
        ):
            return self._groups_cache

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(self.groups_list_url)
                resp.raise_for_status()
                groups = self._js_array_to_json(resp.text, "PNUgroups")
                type(self)._groups_cache = groups
                type(self)._groups_fetched_at = now
                return groups
        except Exception as e:
            print(f"Groups list fetch error: {e}", flush=True)
            return self._groups_cache

    async def _resolve_group_url(self, target: str) -> tuple[Optional[str], bool]:
        """
        Повертає (url_index_js, found_in_groups_list).
        Якщо група знайдена в PNUgroups — повертає її персональний URL і True.
        """
        norm_target = re.sub(r"\s+", "", (target or "")).lower()
        if not norm_target:
            return self.api_endpoint, False

        groups = await self._get_all_groups()
        for g in groups:
            g_label = re.sub(r"\s+", "", str(g.get("l") or "")).lower()
            if g_label == norm_target and g.get("f") is not None and g.get("i") is not None:
                return self._build_group_url(g["f"], g["i"]), True

        return None, False

    def _parse_pnu_js(self, js_text: str, target_date: date, target: Optional[str] = None, filter_by_target: bool = False) -> List[str]:
        """Повертає відформатовані пари на потрібну дату."""
        items = self._js_array_to_json(js_text, "PNUschedule")
        date_str = target_date.isoformat()

        norm_target = re.sub(r"\s+", "", (target or "")).lower()
        day_items = []

        for row in items:
            if row.get("d") != date_str:
                continue
            if filter_by_target and norm_target:
                group_val = re.sub(r"\s+", "", str(row.get("g") or "")).lower()
                teacher_val = re.sub(r"\s+", "", str(row.get("t") or "")).lower()
                if norm_target not in group_val and norm_target not in teacher_val:
                    continue
            day_items.append(row)

        day_items.sort(key=lambda r: (r.get("ls") or "", r.get("li") or 0))

        lessons = []
        for row in day_items:
            time_range = f"{row.get('ls', '')}–{row.get('le', '')}".strip("–")
            subj = row.get("l", "Заняття")
            ltype = f" ({row['lt']})" if row.get("lt") else ""
            group_info = f" [{row['g']}]" if row.get("g") else ""
            teacher = f" — {row['t']}" if row.get("t") else ""
            room = f" [{row['vr']}]" if row.get("vr") else ""

            parts = [p for p in [time_range, f"{subj}{ltype}{group_info}"] if p]
            line = " — ".join(parts) + teacher + room
            lessons.append(line)

        return lessons

    async def get_daily_schedule(self, target: str, target_date: date) -> dict:
        cache_key = f"{target}_{target_date.isoformat()}"
        cached = self.db.query(ScheduleCache).filter_by(cache_key=cache_key).first()

        if cached and cached.updated_at:
            age = datetime.utcnow() - cached.updated_at
            if age < timedelta(minutes=self.cache_timeout_minutes):
                return {
                    "target": target,
                    "date": target_date,
                    "lessons": cached.schedule_data.get("lessons", []),
                }

        resolved_url, found_group = await self._resolve_group_url(target)
        norm_target = re.sub(r"\s+", "", (target or "")).lower()

        # Якщо шукали конкретну групу, але її немає у PNUgroups — перевіряємо як викладача/фільтр у поточному джерелі
        url = resolved_url or self.api_endpoint
        if not url.endswith(".js"):
            url = f"{url.rstrip('/')}/index.js"

        should_filter = (not found_group) and bool(norm_target) and norm_target not in ("іпз-31", "ipz-31")

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                lessons = self._parse_pnu_js(
                    resp.text,
                    target_date,
                    target=target,
                    filter_by_target=should_filter,
                )
                data = {"lessons": lessons}
        except Exception as e:
            print(f"Schedule parse error: {e}", flush=True)
            data = {"lessons": [f"⚠️ Не вдалося завантажити розклад ({e})"]}

        if cached:
            cached.schedule_data = data
        else:
            self.db.add(ScheduleCache(cache_key=cache_key, schedule_data=data))
        self.db.commit()

        return {"target": target, "date": target_date, "lessons": data.get("lessons", [])}


# =====================================================================
# 3. МОДЕЛЬ ШІ-КОНСУЛЬТАЦІЇ (RAG + pgvector + Історія)
# =====================================================================
class AiConsultationModel:
    def __init__(self, db: Session):
        self.db = db
        gemini_key = os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=gemini_key) if gemini_key else None
        self.groq_api_key = os.getenv("GROQ_API_KEY")

        self.groq_model = "openai/gpt-oss-120b"
        self.chat_models = [
            "gemini-3.8-flash"
        ]
        self.embed_model = "gemini-embedding-001"

    async def _generate_via_groq(
        self, system_instruction: str, user_prompt: str, history: List[ChatMessage]
    ) -> Optional[str]:
        """Генерація відповіді через Groq API (OpenAI-сумісний формат)."""
        if not self.groq_api_key:
            return None

        messages = [{"role": "system", "content": system_instruction}]
        for msg in history:
            role = "assistant" if msg.role == "assistant" else "user"
            messages.append({"role": role, "content": msg.content})
        messages.append({"role": "user", "content": user_prompt})

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.groq_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.groq_model,
                        "messages": messages,
                        "temperature": 0.3,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"Groq API error: {e}", flush=True)
            return None

    async def generate_llm_response(
        self, query: str, context_text: str, history: List[ChatMessage]
    ) -> str:
        """Генерація відповіді: спочатку Groq, при помилці — фолбек на Gemini або RAG-контекст."""
        system_instruction = (
            "Ти — корисний освітній чат-бот університету. "
            "Відповідай українською мовою, чітко та по суті. "
            "Якщо в наданому контексті з бази знань є відповідь — спирайся насамперед на нього. "
            "Якщо контекст порожній або не містить відповіді, чесно скажи про це і дай загальну пораду."
        )

        user_prompt = (
            f"Контекст із документів університету:\n{context_text or 'Документів не знайдено.'}\n\n"
            f"Запитання студента: {query}"
        )

        # 1. Спроба через Groq (миттєва відповідь без 503)
        groq_reply = await self._generate_via_groq(system_instruction, user_prompt, history)
        if groq_reply:
            return groq_reply

        # 2. Фолбек на Gemini API
        last_error = ""
        if self.client:
            contents = []
            for msg in history:
                role = "model" if msg.role == "assistant" else "user"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]))

            for model_name in self.chat_models:
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.3,
                        ),
                    )
                    if response.text:
                        return response.text
                except Exception as e:
                    last_error = str(e)
                    continue

        # 3. Локальний фолбек, якщо обидва хмарні API недоступні
        if context_text:
            return (
                "⚠️ Сервери ШІ зараз перевантажені, але ось що знайдено в базі знань університету:\n\n"
                f"{context_text}"
            )
        return f"⚠️ Не вдалося отримати відповідь від ШІ. Перевірте API-ключі. ({last_error})"

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

    def list_documents(self) -> list[dict]:
        """Повертає список унікальних завантажених документів та кількість чанків у кожному."""
        rows = (
            self.db.query(
                KnowledgeChunk.document_name,
                KnowledgeChunk.source_url,
                func.count(KnowledgeChunk.id).label("chunks_count"),
            )
            .group_by(KnowledgeChunk.document_name, KnowledgeChunk.source_url)
            .order_by(KnowledgeChunk.document_name)
            .all()
        )
        return [
            {
                "document_name": r.document_name,
                "source_url": r.source_url,
                "chunks_count": r.chunks_count,
            }
            for r in rows
        ]

    def delete_document(self, document_name: str) -> int:
        """Видаляє всі чанки вказаного документа з бази знань."""
        deleted = self.db.query(KnowledgeChunk).filter_by(document_name=document_name).delete()
        self.db.commit()
        return deleted
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