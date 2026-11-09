"""AI providers: local Ollama (chat + embeddings) or, when LLM_API_KEY is set, an OpenAI-compatible cloud API (chat only)."""
from __future__ import annotations

import time

import numpy as np
import requests


class OllamaUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, chat_model: str, embed_model: str, timeout: float = 120, retries: int = 2,
                 num_ctx: int = 4096, temperature: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.chat_model, self.embed_model = chat_model, embed_model
        self.timeout, self.retries = timeout, retries
        self.options = {"num_ctx": num_ctx, "temperature": temperature}

    @classmethod
    def from_settings(cls, s) -> "OllamaClient":
        return cls(s.ollama_base_url, s.chat_model, s.embed_model, s.ollama_timeout, s.ollama_retries, s.num_ctx, s.temperature)

    def _req(self, method: str, path: str, json: dict | None = None, timeout: float | None = None) -> dict:
        last = None
        for i in range(self.retries + 1):
            try:
                r = requests.request(method, self.base_url + path, json=json, timeout=timeout or self.timeout)
                if r.status_code == 404 and json and "model" in json:
                    raise OllamaUnavailable(f"Model '{json['model']}' not found. Run: ollama pull {json['model']}")
                r.raise_for_status()
                return r.json()
            except OllamaUnavailable:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if i < self.retries:
                    time.sleep(0.5 * (i + 1))
        raise OllamaUnavailable(f"Ollama not reachable at {self.base_url}: {type(last).__name__}")

    def list_models(self) -> list[str]:
        return [m["name"] for m in self._req("GET", "/api/tags", timeout=3).get("models", [])]

    @staticmethod
    def _has(models: list[str], name: str) -> bool:
        base = name if ":" in name else name + ":latest"
        return name in models or base in models

    def status(self) -> dict:
        try:
            models = self.list_models()
        except OllamaUnavailable as exc:
            return {"reachable": False, "models": [], "chat_ok": False, "embed_ok": False, "error": str(exc),
                    "instructions": ["Install Ollama from https://ollama.com (during setup, while online)", "Start it: ollama serve",
                                     f"ollama pull {self.chat_model}", f"ollama pull {self.embed_model}"]}
        chat_ok, embed_ok = self._has(models, self.chat_model), self._has(models, self.embed_model)
        instr = [f"ollama pull {m}" for m, ok in ((self.chat_model, chat_ok), (self.embed_model, embed_ok)) if not ok]
        return {"reachable": True, "models": models, "chat_ok": chat_ok, "embed_ok": embed_ok, "error": None, "instructions": instr}

    def embed(self, texts: list[str]) -> np.ndarray:
        out = self._req("POST", "/api/embed", {"model": self.embed_model, "input": texts})
        v = np.asarray(out["embeddings"], dtype="float32")
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        return v

    def chat(self, system: str, user: str) -> str:
        out = self._req("POST", "/api/chat", {"model": self.chat_model, "stream": False, "options": self.options,
                                              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return out["message"]["content"]


class ApiChatClient:
    """Cloud AI through any OpenAI-compatible API (Groq free tier, Hugging Face router, OpenRouter...).
    Chat only: the knowledge base then uses keyword (BM25) search, which needs no embeddings."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60, temperature: float = 0.1):
        self.base_url, self.api_key, self.chat_model, self.timeout, self.temperature = base_url.rstrip("/"), api_key, model, timeout, temperature
        self.embed_model = None

    def status(self) -> dict:
        return {"reachable": True, "provider": "api", "models": [self.chat_model], "chat_ok": True, "embed_ok": False, "error": None,
                "instructions": []}

    def embed(self, texts: list[str]) -> np.ndarray:
        raise OllamaUnavailable("Cloud mode uses keyword search (no embeddings).")

    def chat(self, system: str, user: str) -> str:
        try:
            r = requests.post(self.base_url + "/chat/completions", timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"},
                              json={"model": self.chat_model, "temperature": self.temperature,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise OllamaUnavailable(f"AI service not reachable ({type(exc).__name__}). Check LLM_API_KEY / LLM_MODEL.") from exc


def make_client(s):
    """Cloud API when LLM_API_KEY is set, otherwise local Ollama."""
    if getattr(s, "llm_api_key", ""):
        return ApiChatClient(s.llm_api_base, s.llm_api_key, s.llm_model)
    return OllamaClient.from_settings(s)
