"""Optional FastAPI service boundary for the existing desktop assistant."""
from __future__ import annotations

from typing import Any

from .core.config import AppConfig
from .core.database import Database
from .core.events import EventBus

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - dependency is optional for desktop-only use
    FastAPI = None
    WebSocket = WebSocketDisconnect = Any
    BaseModel = object


class ChatMessage(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)


def create_app(config: AppConfig | None = None):
    """Create the optional API without starting a second desktop runtime."""
    if FastAPI is None:
        raise RuntimeError("FastAPI and Pydantic are required for the service API")
    settings = config or AppConfig.from_environment()
    database = Database(settings.database_path)
    database.initialize()
    events = EventBus()
    app = FastAPI(title="JARVIS Service API", version="1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "jarvis"}

    @app.get("/api/conversations")
    async def conversations(limit: int = 50) -> list[dict[str, Any]]:
        return database.recent_messages(limit)

    @app.post("/api/conversations")
    async def add_conversation(message: ChatMessage) -> dict[str, int]:
        message_id = database.add_message(message.role, message.content)
        payload = message.model_dump() if hasattr(message, "model_dump") else message.dict()
        await events.publish("conversation.message", payload)
        return {"id": message_id}

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        try:
            while True:
                payload = await socket.receive_json()
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                    await socket.send_json({"error": "content must be a string"})
                    continue
                database.add_message("user", payload["content"])
                await socket.send_json({"type": "ack", "content": payload["content"]})
        except WebSocketDisconnect:
            return

    return app
