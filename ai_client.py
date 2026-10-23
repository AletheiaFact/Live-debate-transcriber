"""Ollama and OpenAI through one SDK, without tools or spreadsheet access."""
import json
import os

import audit_trace as audit


class AIError(ValueError):
    pass


class Client:
    def __init__(self, cfg, sdk=None):
        self.cfg = cfg
        self.sdk = sdk
        self.model_checked = False

    def connect(self):
        if self.sdk is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise AIError("instale requirements.txt para usar IA") from exc
            key = os.environ.get("OPENAI_API_KEY") if self.cfg.provider == "openai" else "ollama"
            if not key:
                raise AIError("OPENAI_API_KEY ausente")
            self.sdk = OpenAI(base_url=self.cfg.base_url, api_key=key,
                              timeout=self.cfg.timeout_seconds, max_retries=0)
        return self.sdk

    def check(self):
        try:
            models = [m.id for m in self.connect().models.list().data]
        except AIError:
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            if code == 401:
                raise AIError("chave de IA rejeitada pelo provedor (HTTP 401); verifique OPENAI_API_KEY") from exc
            if code == 403:
                raise AIError("acesso ao provedor de IA negado (HTTP 403); verifique as permissões da chave") from exc
            raise AIError("não foi possível consultar o provedor de IA; verifique rede, chave e servidor") from exc
        if self.cfg.model not in models:
            raise AIError(f"modelo configurado indisponível: {self.cfg.model}")
        self.model_checked = True
        return models

    def structured(self, system, data, schema):
        with audit.step("Chamada ao modelo: " + schema.__name__, actor="Model", schema_name=schema.__name__):
            return self._structured(system, data, schema)

    def _structured(self, system, data, schema):
        if not self.model_checked:
            self.check()
        contract = schema.model_json_schema()
        fmt = {"type": "json_object"}
        if self.cfg.json_mode == "json_schema":
            fmt = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": contract}}
        try:
            options = {}
            if self.cfg.temperature is not None:
                options["temperature"] = self.cfg.temperature
            if self.cfg.provider == "ollama" and self.cfg.ollama_reasoning_effort:
                options["reasoning_effort"] = self.cfg.ollama_reasoning_effort
            request = dict(model=self.cfg.model,
                           messages=[{"role": "system", "content": system + "\nResponda apenas JSON conforme o schema: " + json.dumps(contract)},
                                     {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                           response_format=fmt, max_tokens=6000, **options)
            audit.emit("model_request", request=request, prompt_sha256=audit.digest(request["messages"]),
                       schema_sha256=audit.digest(contract))
            response = self.connect().chat.completions.create(**request)
            choice = response.choices[0]
            content = choice.message.content
            refusal = getattr(choice.message, "refusal", None)
            usage = getattr(response, "usage", None)
            audit.emit("model_response", content=content[:200000] if isinstance(content, str) else None,
                       content_characters=len(content) if isinstance(content, str) else 0,
                       capture_truncated=isinstance(content, str) and len(content) > 200000,
                       finish_reason=choice.finish_reason,
                       refusal=refusal[:10000] if isinstance(refusal, str) else None,
                       resolved_model=getattr(response, "model", None) if isinstance(getattr(response, "model", None), str) else None,
                       response_id=getattr(response, "id", None) if isinstance(getattr(response, "id", None), str) else None,
                       usage={k: v for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                              if type(v := getattr(usage, k, None)) is int})
            accepted = choice.finish_reason == "stop" and not refusal
            audit.check("response_complete", accepted, "Resposta finalizada sem recusa.")
            if not accepted:
                raise AIError("resposta de IA recusada ou incompleta")
            audit.check("response_size", bool(content) and len(content) <= 100000, "Resposta entre 1 e 100000 caracteres.")
            if not content or len(content) > 100000:
                raise AIError("resposta de IA vazia ou acima do limite")
            parsed = schema.model_validate_json(content)
            audit.check("json_schema", True, "JSON validado pelo schema estrito.")
            audit.emit("parsed_output", output=parsed.model_dump())
            return parsed
        except AIError:
            raise
        except Exception as exc:
            # Never expose SDK exception text: it can contain payloads or credentials.
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                audit.check("json_schema", False, "JSON rejeitado pelo schema estrito.",
                            validation_errors=[{"path": list(e["loc"]), "type": e["type"]}
                                               for e in exc.errors(include_input=False, include_context=False, include_url=False)])
                raise AIError("JSON de IA inválido para o schema") from exc
            if type(exc).__name__ == "APITimeoutError":
                raise AIError("tempo limite de inferência atingido; verifique a carga do servidor ou aumente AI_EVIDENCE_TIMEOUT_SECONDS") from exc
            raise AIError("falha na inferência; verifique conexão, modelo e AI_EVIDENCE_JSON_MODE") from exc
