"""Environment settings and policy loading (no network access)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_POLICY_KEYS = ["version", "timezone", "team", "submission", "validation", "similarity", "review", "rag", "domains"]


def load_env(path: Path = ROOT / ".env") -> None:
    if os.environ.get("INCUB_NO_DOTENV") == "1":  # the automatic tests must never use the real .env (real email account!)
        return
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass
class Settings:
    db_path: Path
    storage_dir: Path
    kb_dir: Path
    index_dir: Path
    policy_file: Path
    ollama_base_url: str
    chat_model: str
    embed_model: str
    ollama_timeout: float
    ollama_retries: int
    num_ctx: int
    temperature: float
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_tls: bool
    smtp_ssl: bool = False
    signup_code: str | None = None
    email_provider: str = "smtp"              # "smtp" (Gmail/Outlook/college server) or "brevo" (HTTPS API, works where SMTP ports are blocked)
    brevo_api_key: str | None = None
    brevo_api_url: str = "https://api.brevo.com/v3/smtp/email"
    email_from_name: str = "Incubation Portal"
    llm_api_key: str = ""                    # cloud AI (OpenAI-compatible, e.g. Groq free tier); empty = use local Ollama
    llm_api_base: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.1-8b-instant"
    backup_dir: Path | None = None            # persistent copy of the data (Hugging Face Storage Bucket); None = not used


def _p(v: str) -> Path:
    p = Path(v)
    return p if p.is_absolute() else ROOT / p


def get_settings() -> Settings:
    load_env()
    e = os.environ.get
    return Settings(
        db_path=_p(e("INCUB_DB_PATH", "storage/incubation.db")),
        storage_dir=_p(e("INCUB_STORAGE_DIR", "storage")),
        kb_dir=_p(e("INCUB_KB_DIR", "kb")),
        index_dir=_p(e("INCUB_INDEX_DIR", "storage/index")),
        policy_file=_p(e("INCUB_POLICY_FILE", "config/policy.json")),
        ollama_base_url=e("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        chat_model=e("OLLAMA_CHAT_MODEL", "mistral"),
        embed_model=e("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        ollama_timeout=float(e("OLLAMA_TIMEOUT", "120")),
        ollama_retries=int(e("OLLAMA_RETRIES", "2")),
        num_ctx=int(e("OLLAMA_NUM_CTX", "4096")),
        temperature=float(e("OLLAMA_TEMPERATURE", "0.1")),
        smtp_host=e("SMTP_HOST") or None,
        smtp_port=int(e("SMTP_PORT", "587")),
        smtp_user=e("SMTP_USER") or None,
        smtp_password=e("SMTP_PASSWORD") or None,
        smtp_from=e("SMTP_FROM") or e("EMAIL_FROM") or None,
        smtp_tls=e("SMTP_TLS", "true").lower() == "true",
        smtp_ssl=e("SMTP_SSL", "false").lower() == "true",
        signup_code=e("SIGNUP_CODE") or None,
        email_provider=(e("EMAIL_PROVIDER", "smtp") or "smtp").strip().lower(),
        brevo_api_key=e("BREVO_API_KEY") or None,
        brevo_api_url=e("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email"),
        email_from_name=e("EMAIL_FROM_NAME", "Incubation Portal"),
        llm_api_key=e("LLM_API_KEY", ""),
        llm_api_base=e("LLM_API_BASE", "https://api.groq.com/openai/v1"),
        llm_model=e("LLM_MODEL", "llama-3.1-8b-instant"),
        backup_dir=_p(e("INCUB_BACKUP_DIR")) if e("INCUB_BACKUP_DIR") else None,
    )


def load_policy(path: Path | None = None) -> dict:
    path = path or get_settings().policy_file
    raw = Path(path).read_text(encoding="utf-8")
    policy = json.loads(raw)
    missing = [k for k in REQUIRED_POLICY_KEYS if k not in policy]
    if missing:
        raise ValueError(f"Policy file missing keys: {missing}")
    if policy["team"]["min_members"] > policy["team"]["max_members"]:
        raise ValueError("team.min_members > team.max_members")
    policy["_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    public = os.environ.get("INCUB_PUBLIC_URL", "").strip()
    if public:  # the address put into emails (e.g. https://user-space.hf.space); an admin can still change it in the app
        policy.setdefault("notifications", {})["app_url"] = public.rstrip("/")
    return policy
