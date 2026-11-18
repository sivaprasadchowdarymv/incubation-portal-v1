"""Several AI API keys, used in order with automatic fallback.

The admin pastes keys from free (or paid) providers in ⚙️ Programme Setup → AI keys. When the first provider is busy
(rate limit), down or rejects the key, the next one is tried, and the failing one rests for a short cooldown.
Every provider speaks the OpenAI-compatible chat API. Keys are stored in the private database and never shown again
in full (only the last 4 characters)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import requests

from rag.ollama import OllamaUnavailable

# name -> (base URL, a suggested free/cheap model, where to get a key). Models change over time: edit them in the table.
PRESETS = {
    "Groq (accurate, 70B)": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "https://console.groq.com/keys"),
    "Groq (fast, 8B)": ("https://api.groq.com/openai/v1", "llama-3.1-8b-instant", "https://console.groq.com/keys"),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "https://aistudio.google.com/apikey"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", "https://openrouter.ai/keys"),
    "Hugging Face": ("https://router.huggingface.co/v1", "meta-llama/Llama-3.1-8B-Instruct", "https://huggingface.co/settings/tokens"),
    "Mistral": ("https://api.mistral.ai/v1", "mistral-small-latest", "https://console.mistral.ai/api-keys"),
    "Cerebras": ("https://api.cerebras.ai/v1", "llama3.1-8b", "https://cloud.cerebras.ai"),
    "Together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free", "https://api.together.ai/settings/api-keys"),
    "Custom (OpenAI-compatible)": ("", "", ""),
}
COOLDOWN_RATE_LIMIT = 60      # seconds a provider rests after "too many requests"
COOLDOWN_ERROR = 300          # seconds after any other failure
TIMEOUT = (5, 25)             # (connect, read) seconds: a slow provider is skipped quickly to the next key
MAX_TOKENS = 500              # short answers are faster and stay on topic


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def mask(key: str) -> str:
    return ("•" * 8 + key[-4:]) if key and len(key) > 4 else "••••"


def _body(model: str, system: str, user: str, temperature: float, stream: bool = False) -> dict:
    return {"model": model, "temperature": temperature, "max_tokens": MAX_TOKENS, "stream": stream,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}


def _call(base_url: str, api_key: str, model: str, system: str, user: str, timeout=TIMEOUT, temperature: float = 0.1) -> str:
    r = requests.post(base_url.rstrip("/") + "/chat/completions", timeout=timeout, headers={"Authorization": f"Bearer {api_key}"},
                      json=_body(model, system, user, temperature))
    if r.status_code >= 400:
        raise ProviderError(r.status_code, (r.text or "")[:200])
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise ProviderError(0, "unexpected answer format") from exc


def _stream(base_url: str, api_key: str, model: str, system: str, user: str, timeout=TIMEOUT, temperature: float = 0.1):
    """Yield pieces of the answer as the provider writes them (OpenAI-compatible server-sent events)."""
    r = requests.post(base_url.rstrip("/") + "/chat/completions", timeout=timeout, stream=True,
                      headers={"Authorization": f"Bearer {api_key}"}, json=_body(model, system, user, temperature, stream=True))
    if r.status_code >= 400:
        raise ProviderError(r.status_code, (r.text or "")[:200])
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            piece = json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
        except (ValueError, KeyError, IndexError):
            continue
        if piece:
            yield piece


class ProviderError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status


# ---------------- admin management (called from the UI) ----------------

def _admin(conn, user):
    from workflow.services import authorize
    return authorize(conn, user, "admin", action="ai_providers")


def list_providers(conn, admin) -> list[dict]:
    _admin(conn, admin)
    return [dict(r) | {"api_key": mask(r["api_key"])} for r in conn.execute("SELECT * FROM ai_providers ORDER BY position, id")]


def add_provider(conn, admin, name: str, base_url: str, api_key: str, model: str) -> int:
    from workflow.services import WorkflowError, audit
    _admin(conn, admin)
    name, base_url, api_key, model = (name or "").strip(), (base_url or "").strip().rstrip("/"), (api_key or "").strip(), (model or "").strip()
    if not name or not model:
        raise WorkflowError("Give the provider a name and a model.")
    if not base_url.startswith("https://"):
        raise WorkflowError("The API address must start with https://")
    if len(api_key) < 8 or any(c.isspace() for c in api_key):
        raise WorkflowError("Paste the whole API key (no spaces).")
    pos = (conn.execute("SELECT MAX(position) FROM ai_providers").fetchone()[0] or 0) + 1
    pid = conn.execute("INSERT INTO ai_providers(name, base_url, api_key, model, position, enabled, created_at) VALUES (?,?,?,?,?,1,?)",
                       (name[:60], base_url, api_key, model[:120], pos, _iso(_now()))).lastrowid
    audit(conn, admin, "ai_provider_added", "ai_provider", pid, {"name": name, "model": model})
    return pid


def update_provider(conn, admin, provider_id: int, *, enabled: bool | None = None, model: str | None = None, api_key: str | None = None) -> None:
    from workflow.services import WorkflowError, audit
    _admin(conn, admin)
    if not conn.execute("SELECT 1 FROM ai_providers WHERE id=?", (provider_id,)).fetchone():
        raise WorkflowError("Provider not found")
    if enabled is not None:
        conn.execute("UPDATE ai_providers SET enabled=?, cooldown_until=NULL WHERE id=?", (int(bool(enabled)), provider_id))
    if model:
        conn.execute("UPDATE ai_providers SET model=? WHERE id=?", (model.strip()[:120], provider_id))
    if api_key:
        if len(api_key.strip()) < 8:
            raise WorkflowError("Paste the whole API key.")
        conn.execute("UPDATE ai_providers SET api_key=?, cooldown_until=NULL, last_error=NULL WHERE id=?", (api_key.strip(), provider_id))
    audit(conn, admin, "ai_provider_updated", "ai_provider", provider_id, {"enabled": enabled, "model": model, "key_replaced": bool(api_key)})


def move_provider(conn, admin, provider_id: int, up: bool) -> None:
    _admin(conn, admin)
    rows = [r["id"] for r in conn.execute("SELECT id FROM ai_providers ORDER BY position, id")]
    if provider_id not in rows:
        return
    i = rows.index(provider_id)
    j = i - 1 if up else i + 1
    if 0 <= j < len(rows):
        rows[i], rows[j] = rows[j], rows[i]
    for pos, pid in enumerate(rows, 1):
        conn.execute("UPDATE ai_providers SET position=? WHERE id=?", (pos, pid))


def delete_provider(conn, admin, provider_id: int) -> None:
    from workflow.services import audit
    _admin(conn, admin)
    conn.execute("DELETE FROM ai_providers WHERE id=?", (provider_id,))
    audit(conn, admin, "ai_provider_deleted", "ai_provider", provider_id)


def test_provider(conn, admin, provider_id: int) -> tuple[bool, str]:
    _admin(conn, admin)
    r = conn.execute("SELECT * FROM ai_providers WHERE id=?", (provider_id,)).fetchone()
    t0 = time.perf_counter()
    try:
        out = _call(r["base_url"], r["api_key"], r["model"], "You are a test.", "Reply with the single word OK.")
        conn.execute("UPDATE ai_providers SET last_ok_at=?, last_error=NULL, cooldown_until=NULL WHERE id=?", (_iso(_now()), provider_id))
        return True, f"Works ({(time.perf_counter() - t0) * 1000:.0f} ms): {out.strip()[:40]}"
    except (ProviderError, requests.RequestException) as exc:
        conn.execute("UPDATE ai_providers SET last_error=? WHERE id=?", (str(exc)[:300], provider_id))
        return False, f"Failed: {exc}"


# ---------------- the client the portal uses ----------------

class MultiApiChatClient:
    """Tries each enabled provider in order; a failing one rests (cooldown) and the next is used."""

    embed_model = None

    def __init__(self, db_path, extra: list[dict] | None = None):
        self.db_path = db_path
        self.extra = extra or []          # e.g. LLM_API_KEY from Secrets, used after the database keys
        self.chat_model = ", ".join(p["model"] for p in self._providers()[:2]) or "none"
        self.last_used: str | None = None

    def _conn(self):
        from database.db import connect
        return connect(self.db_path)

    def _providers(self) -> list[dict]:
        c = self._conn()
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM ai_providers WHERE enabled=1 ORDER BY position, id")]
        finally:
            c.close()
        return rows + self.extra

    def status(self) -> dict:
        n = len(self._providers())
        return {"reachable": n > 0, "provider": "api", "models": [p["model"] for p in self._providers()], "chat_ok": n > 0,
                "embed_ok": False, "error": None if n else "No AI keys configured", "instructions": [] if n else ["Add an AI key in Programme Setup → AI keys."]}

    def embed(self, texts):
        raise OllamaUnavailable("Cloud AI uses keyword search (no embeddings).")

    def chat(self, system: str, user: str) -> str:
        errors = []
        now = _now()
        for p in self._providers():
            if p.get("cooldown_until") and datetime.fromisoformat(p["cooldown_until"]) > now:
                errors.append(f"{p['name']}: resting")
                continue
            try:
                out = _call(p["base_url"], p["api_key"], p["model"], system, user)
                self._record(p, ok=True)
                self.last_used = p["name"]
                return out
            except (ProviderError, requests.RequestException) as exc:
                wait = COOLDOWN_RATE_LIMIT if getattr(exc, "status", 0) == 429 else COOLDOWN_ERROR
                self._record(p, ok=False, error=str(exc), cooldown=now + timedelta(seconds=wait))
                errors.append(f"{p['name']}: {str(exc)[:80]}")
        raise OllamaUnavailable("All AI keys are busy or failing (" + "; ".join(errors) + "). Try again in a minute, or add another key.")

    def chat_stream(self, system: str, user: str):
        """Stream from the first provider that answers; fall back to the next one if a provider fails before its first words."""
        errors = []
        now = _now()
        for p in self._providers():
            if p.get("cooldown_until") and datetime.fromisoformat(p["cooldown_until"]) > now:
                errors.append(f"{p['name']}: resting")
                continue
            started = False
            try:
                for piece in _stream(p["base_url"], p["api_key"], p["model"], system, user):
                    started = True
                    yield piece
                self._record(p, ok=True)
                self.last_used = p["name"]
                return
            except (ProviderError, requests.RequestException) as exc:
                wait = COOLDOWN_RATE_LIMIT if getattr(exc, "status", 0) == 429 else COOLDOWN_ERROR
                self._record(p, ok=False, error=str(exc), cooldown=now + timedelta(seconds=wait))
                if started:   # words were already shown: don't mix two providers' answers
                    raise OllamaUnavailable(f"{p['name']} stopped mid-answer; please ask again.") from exc
                errors.append(f"{p['name']}: {str(exc)[:80]}")
        raise OllamaUnavailable("All AI keys are busy or failing (" + "; ".join(errors) + "). Try again in a minute, or add another key.")

    def _record(self, p: dict, ok: bool, error: str = "", cooldown: datetime | None = None) -> None:
        if not p.get("id"):
            return
        c = self._conn()
        try:
            if ok:
                c.execute("UPDATE ai_providers SET last_ok_at=?, last_error=NULL, cooldown_until=NULL WHERE id=?", (_iso(_now()), p["id"]))
            else:
                c.execute("UPDATE ai_providers SET last_error=?, cooldown_until=? WHERE id=?", (error[:300], _iso(cooldown) if cooldown else None, p["id"]))
        finally:
            c.close()


def has_db_providers(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM ai_providers WHERE enabled=1").fetchone())
