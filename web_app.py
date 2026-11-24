import os
import uuid
import httpx
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "edu-bot-dev-secret-key")

# У Docker це буде http://api:8000, локально — http://localhost:8000
FASTAPI_URL = os.getenv("FASTAPI_URL", "http://localhost:8000")


def get_session_id() -> str:
    """Генерує або повертає унікальний session_id студента."""
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]


# --- Сторінки (View) ---

@app.route("/")
def chat_page():
    """Головна сторінка вебчату для студента."""
    return render_template("chat.html", session_id=get_session_id())


@app.route("/admin")
def admin_page():
    """Сторінка адмін-панелі."""
    return render_template("admin.html")


# --- Проксі-маршрути до FastAPI бекенду ---

@app.route("/api/chat/init", methods=["GET"])
def proxy_chat_init():
    sid = get_session_id()
    resp = httpx.get(f"{FASTAPI_URL}/api/chat/init", params={"session_id": sid}, timeout=10.0)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/chat/message", methods=["POST"])
def proxy_chat_message():
    payload = request.get_json() or {}
    payload["session_id"] = get_session_id()
    resp = httpx.post(f"{FASTAPI_URL}/api/chat/message", json=payload, timeout=30.0)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/admin/notifications", methods=["POST"])
def proxy_admin_notifications():
    resp = httpx.post(f"{FASTAPI_URL}/api/admin/notifications", json=request.get_json(), timeout=15.0)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/admin/schedule-config", methods=["PUT"])
def proxy_schedule_config():
    resp = httpx.put(f"{FASTAPI_URL}/api/admin/schedule-config", json=request.get_json(), timeout=15.0)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/admin/knowledge-base", methods=["GET", "POST"])
def proxy_knowledge_base():
    if request.method == "GET":
        resp = httpx.get(f"{FASTAPI_URL}/api/admin/knowledge-base", timeout=15.0)
        return jsonify(resp.json()), resp.status_code

    file = request.files.get("file")
    source_url = request.form.get("source_url", "")
    if not file:
        return jsonify({"detail": "Файл не передано"}), 400

    files = {"file": (file.filename, file.read(), file.mimetype)}
    data = {"source_url": source_url} if source_url else {}
    resp = httpx.post(f"{FASTAPI_URL}/api/admin/knowledge-base", files=files, data=data, timeout=60.0)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/admin/knowledge-base/<path:document_name>", methods=["DELETE"])
def proxy_knowledge_delete(document_name):
    resp = httpx.delete(f"{FASTAPI_URL}/api/admin/knowledge-base/{document_name}", timeout=15.0)
    return jsonify(resp.json()), resp.status_code

@app.route("/api/chat/group", methods=["PUT"])
def proxy_chat_group():
    payload = request.get_json() or {}
    payload["session_id"] = get_session_id()
    resp = httpx.put(f"{FASTAPI_URL}/api/chat/group", json=payload, timeout=15.0)
    return jsonify(resp.json()), resp.status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)