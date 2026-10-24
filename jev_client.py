"""Client for Jev, TypeSafe's evaluation model, through the TypeSafe API.

Ported from ai_task_processor/services/jev_client.py (score and choice
questions), on the standard library like the rest of the recorder and server.

POST {base_url}/v1/systemone with {"state", "model", "questions"} returns, per
question, a confidence and either a score with probabilities per level
("0", "1", ...) or a choice with probabilities per option name.

With the placeholder API key the client returns deterministic mock answers in
the real wire format, so everything after Jev runs at zero cost. An empty key
is an error, so real sessions never store mock results by accident.
"""

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime

TYPESAFE_BASE_URL = "https://api.typesafe.ai"
TYPESAFE_API_KEY_PLACEHOLDER = "your_typesafe_api_key_here"
DEFAULT_MODEL = "jev-1.13.0"
MOCK_MODEL = "jev-mock"
DEFAULT_TIMEOUT = 30
DEFAULT_BACKOFF = [2, 5, 10]  # a live debate cannot wait minutes for one block
USER_AGENT = "debate-transcriber/1.0"

# Overload, rate limiting and upstream hiccups are worth waiting out
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
QUESTION_TYPES = {"score", "choice"}


class JevError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def urllib_transport(url, headers, body, timeout):
    """POST JSON bytes; returns (status, response text). Network errors raise OSError."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def parse_backoff(value):
    """"2,5,10" or "[2, 5, 10]" -> [2.0, 5.0, 10.0]."""
    parts = [p.strip() for p in value.strip().strip("[]").split(",") if p.strip()]
    return [float(p) for p in parts]


class Config:
    def __init__(self):
        self.api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        self.model = os.environ.get("JEV_MODEL", "").strip() or DEFAULT_MODEL
        self.base_url = (os.environ.get("JEV_BASE_URL", "").strip() or TYPESAFE_BASE_URL).rstrip("/")
        self.timeout = float(os.environ.get("JEV_TIMEOUT") or DEFAULT_TIMEOUT)
        backoff = os.environ.get("JEV_BACKOFF_SECONDS", "")
        self.backoff = parse_backoff(backoff) if backoff.strip() else list(DEFAULT_BACKOFF)


class JevClient:
    def __init__(self, cfg, transport=urllib_transport, sleep=time.sleep):
        # Tests inject a fake transport, so no real request is made
        self.cfg = cfg
        self.transport = transport
        self.sleep = sleep

    @property
    def is_mock(self):
        return self.cfg.api_key == TYPESAFE_API_KEY_PLACEHOLDER

    def evaluate(self, state, questions):
        """Ask Jev score or choice questions about the state.

        questions: {name: {"type": "score", "instructions": str, "criteria": [level0, level1, ...]}}
                   or {name: {"type": "choice", "instructions": str, "criteria": {option: description}}}
        Returns {"answers": {name: answer}, "model": str, "usage": {...}}; model is "jev-mock" for mock answers.
        A score answer is {"score", "probabilities": [p0, p1, ...], "confidence"};
        a choice answer is {"choice", "probabilities": {option: p}, "confidence"}, in criteria order.
        """
        for name, question in questions.items():
            if question.get("type") not in QUESTION_TYPES:
                raise JevError(f"unsupported Jev question type for '{name}': {question.get('type')}")
        if self.is_mock:
            # Loud on purpose: a placeholder key in a real session must not go unnoticed
            log(f"WARNING: using mock Jev answers (TYPESAFE_API_KEY is the placeholder), questions={list(questions)}")
            return self._parse(self._mock_response(state, questions), questions, MOCK_MODEL)
        if not self.cfg.api_key:
            raise JevError("TYPESAFE_API_KEY is not configured")

        url = f"{self.cfg.base_url}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        body = json.dumps({"state": state, "model": self.cfg.model, "questions": questions},
                          ensure_ascii=False).encode()
        for attempt in range(len(self.cfg.backoff) + 1):
            try:
                return self._parse(self._post(url, headers, body), questions, self.cfg.model)
            except JevError as e:
                if not e.retryable or attempt >= len(self.cfg.backoff):
                    raise
                delay = self.cfg.backoff[attempt] * (0.8 + random.random() * 0.4)
                log(f"Jev temporary error, retrying in {delay:.1f}s (attempt {attempt + 1}): {e}")
                self.sleep(delay)

    def _post(self, url, headers, body):
        try:
            status, text = self.transport(url, headers, body, self.cfg.timeout)
        except OSError as e:  # includes timeouts and connection errors
            raise JevError(f"Jev connection error: {type(e).__name__}: {e}", retryable=True)
        if status in RETRYABLE_STATUS_CODES:
            raise JevError(f"Jev HTTP {status}: {text[:300]}", retryable=True)
        if status >= 400:
            raise JevError(f"Jev HTTP {status}: {text[:300]}")
        try:
            return json.loads(text)
        except ValueError:
            raise JevError("Jev returned a response that is not JSON")

    @staticmethod
    def _parse(data, questions, model):
        raw_answers = (data or {}).get("answers") or {}
        missing = [name for name in questions if name not in raw_answers]
        if missing:
            raise JevError(f"Jev response is missing answers for: {missing}")
        answers = {}
        for name, question in questions.items():
            raw = raw_answers[name]
            confidence = raw.get("confidence")
            confidence = None if confidence is None else float(confidence)
            if question["type"] == "choice":
                answers[name] = JevClient._parse_choice(name, raw, list(question["criteria"]), confidence)
                continue
            levels = len(question["criteria"])
            probabilities = raw.get("probabilities") or {}
            try:
                ordered = [float(probabilities[str(i)]) for i in range(levels)]
                score = float(raw["score"])
            except (KeyError, TypeError, ValueError):
                raise JevError(f"Jev score answer '{name}' expected {levels} levels, got {raw!r:.300}")
            answers[name] = {"score": score, "probabilities": ordered, "confidence": confidence}
        usage = (data or {}).get("usage") or {}
        return {"answers": answers, "model": (data or {}).get("model") or model,
                "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}}

    @staticmethod
    def _parse_choice(name, raw, options, confidence):
        probabilities = raw.get("probabilities") or {}
        try:
            ordered = {option: float(probabilities[option]) for option in options}
        except (KeyError, TypeError, ValueError):
            raise JevError(f"Jev choice answer '{name}' expected probabilities for {options}, got {raw!r:.300}")
        if raw.get("choice") not in ordered:
            raise JevError(f"Jev choice answer '{name}' chose {raw.get('choice')!r}, not one of {options}")
        return {"choice": raw["choice"], "probabilities": ordered, "confidence": confidence}

    @staticmethod
    def _mock_response(state, questions):
        """Wire-format answers seeded by the state: same text, same answer."""
        state_key = json.dumps(state, sort_keys=True, ensure_ascii=False)
        answers = {}
        for name, question in questions.items():
            rng = random.Random(hashlib.sha256(f"{name}:{state_key}".encode()).hexdigest())
            size = len(question["criteria"])
            peak = rng.randrange(size)  # one clear peak, like Jev usually returns
            weights = [3.0 if i == peak else rng.uniform(0.05, 0.6) for i in range(size)]
            probabilities = [w / sum(weights) for w in weights]
            # TypeSafe's definition: (count * peak - 1) / (count - 1)
            confidence = (size * max(probabilities) - 1) / (size - 1) if size > 1 else 1.0
            if question["type"] == "choice":
                options = list(question["criteria"])
                answers[name] = {"choice": options[peak], "confidence": confidence,
                                 "probabilities": dict(zip(options, probabilities))}
                continue
            answers[name] = {
                "score": sum(i * p for i, p in enumerate(probabilities)),
                "probabilities": {str(i): p for i, p in enumerate(probabilities)},
                "confidence": confidence,
            }
        return {"answers": answers, "model": MOCK_MODEL, "usage": {}}
