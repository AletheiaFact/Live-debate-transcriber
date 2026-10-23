"""Environment configuration, independent of the transcript's send destination."""
import json
import math
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    enabled: bool = False
    provider: str = "ollama"
    base_url: str = "http://192.168.0.180:11434/v1"
    model: str = "qwen3.5:4b"
    json_mode: str = "json_schema"
    ollama_reasoning_effort: str = "none"
    temperature: float | None = 0.0
    timeout_seconds: float = 90
    max_candidates: int = 10
    cache_ttl_seconds: int = 300
    max_rows_per_tab: int = 10000
    spreadsheet_id: str = "1rY0NKhb_n2meBSTfAYFXEsUoJaxZku31XZMs-qVNc3k"
    credentials_path: str = ""
    weights: dict = field(default_factory=dict)
    topic_teams: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        provider = os.environ.get("AI_EVIDENCE_PROVIDER", "ollama").strip().lower()
        if provider not in {"ollama", "openai"}:
            raise ValueError("AI_EVIDENCE_PROVIDER: use ollama ou openai")
        prefix = "AI_EVIDENCE_" + provider.upper()
        cfg = cls(
            enabled=os.environ.get("AI_EVIDENCE_ENABLED", "false").lower() in {"1", "true", "yes"},
            provider=provider,
            base_url=os.environ.get(prefix + "_BASE_URL", cls.base_url if provider == "ollama" else "https://api.openai.com/v1").rstrip("/"),
            model=os.environ.get(prefix + "_MODEL", cls.model if provider == "ollama" else "gpt-4.1-mini"),
            json_mode=os.environ.get("AI_EVIDENCE_JSON_MODE", "json_schema"),
            ollama_reasoning_effort=os.environ.get("AI_EVIDENCE_OLLAMA_REASONING_EFFORT", "none"),
            temperature=float(os.environ.get("AI_EVIDENCE_TEMPERATURE", "0")) if os.environ.get("AI_EVIDENCE_TEMPERATURE", "0").strip() else None,
            timeout_seconds=float(os.environ.get("AI_EVIDENCE_TIMEOUT_SECONDS", "90")),
            max_candidates=int(os.environ.get("AI_EVIDENCE_MAX_CANDIDATES", "10")),
            cache_ttl_seconds=int(os.environ.get("AI_EVIDENCE_CACHE_TTL_SECONDS", "300")),
            max_rows_per_tab=int(os.environ.get("AI_EVIDENCE_MAX_ROWS_PER_TAB", "10000")),
            spreadsheet_id=os.environ.get("AI_EVIDENCE_SPREADSHEET_ID", cls.spreadsheet_id),
            credentials_path=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
            weights=json.loads(os.environ.get("AI_EVIDENCE_WEIGHTS", "{}")),
            topic_teams=json.loads(os.environ.get("AI_EVIDENCE_TOPIC_TEAMS", "{}")),
        )
        if not 1 <= cfg.max_candidates <= 20 or not 1 <= cfg.timeout_seconds <= 300:
            raise ValueError("limites de IA inválidos (candidatos 1–20; timeout 1–300s)")
        if not 0 <= cfg.cache_ttl_seconds <= 3600 or not 2 <= cfg.max_rows_per_tab <= 50000:
            raise ValueError("limites de cache inválidos")
        if cfg.json_mode not in {"json_schema", "json_object"}:
            raise ValueError("AI_EVIDENCE_JSON_MODE: use json_schema ou json_object")
        if cfg.temperature is not None and not 0 <= cfg.temperature <= 2:
            raise ValueError("temperatura inválida")
        if not isinstance(cfg.weights, dict) or not isinstance(cfg.topic_teams, dict):
            raise ValueError("pesos e roteamento devem ser objetos JSON")
        allowed = {"tracking": {"L", "G", "F", "T", "N"}, "narratives": {"C", "D", "H"}, "plans": {"I", "F", "E", "J"}}
        for kind, weights in cfg.weights.items():
            if kind not in allowed or not isinstance(weights, dict):
                raise ValueError("tipo de aba inválido nos pesos de busca")
            for column, weight in weights.items():
                if column not in allowed[kind] or type(weight) not in {int, float} or not math.isfinite(weight) or not 0 <= weight <= 20:
                    raise ValueError("coluna ou peso de busca inválido")
        for topic, teams in cfg.topic_teams.items():
            values = teams if isinstance(teams, list) else [teams]
            if not topic.strip() or not all(isinstance(t, str) and t in {"F1", "F2", "F3", "F4", "L1", "L2"} for t in values):
                raise ValueError("roteamento de equipe inválido")
        return cfg
