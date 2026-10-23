"""Validated, bounded contracts for AI evidence v1 (Pydantic 2)."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Verdict = Literal["VERDADEIRO", "FALSO", "IMPRECISO", "INSUFICIENTE"]
Relevance = Literal["DIRECT_MATCH", "PARTIAL_MATCH", "BACKGROUND_ONLY", "NOT_RELEVANT"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Segment(StrictModel):
    segment_id: str = Field(min_length=1, max_length=200)
    speaker: str = Field(max_length=200)
    transcription: str = Field(min_length=1, max_length=16000)
    start_time: float | None = None
    end_time: float | None = None
    language: str = Field(default="pt-BR", max_length=30)

    @model_validator(mode="after")
    def time_order(self):
        if self.start_time is not None and self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("fim anterior ao início")
        return self


class ExtractedClaim(StrictModel):
    quote: str = Field(min_length=1, max_length=16000)
    normalized_claim: str = Field(min_length=1, max_length=2000)
    checkable: bool
    subjects: list[str] = Field(max_length=8)
    topic: str = Field(max_length=200)
    keywords: list[str] = Field(max_length=20)
    synonyms: list[str] = Field(max_length=20)


class Extraction(StrictModel):
    claims: list[ExtractedClaim] = Field(max_length=12)


class RecordAssessment(StrictModel):
    evidence_key: str
    relevance: Relevance
    summary: str = Field(max_length=2000)
    context_compatible: bool
    source_quality: Literal["RELIABLE", "UNCERTAIN"]
    # Must be copied verbatim from this record's data summary, never its verdict.
    basis_quote: str = Field(max_length=3000)


class Assessment(StrictModel):
    records: list[RecordAssessment] = Field(max_length=20)
    suggested_verdict: Verdict | None
    verdict_evidence_keys: list[str] = Field(max_length=20)
    explanation: str = Field(min_length=1, max_length=4000)


class Evidence(StrictModel):
    evidence_key: str
    spreadsheet_id: str
    sheet_name: str
    sheet_id: int
    record_id: str
    row_number: int = Field(ge=2)
    cell_range: str
    spreadsheet_row_url: str
    source_urls: list[str]
    primary_source_urls: list[str]
    fields: dict[str, str]
    team: str
    record_type: str
    existing_label: str
    score: float
    relevance: Relevance = "BACKGROUND_ONLY"
    summary: str = "Avaliação pendente."
    context_compatible: bool | None = None
    source_quality: Literal["RELIABLE", "UNCERTAIN"] | None = None
    basis_quote: str = ""
    retrieved_at: str
    cache_status: Literal["FRESH", "STALE"]


class ClaimResult(ExtractedClaim):
    claim_id: str
    segment_id: str
    speaker: str
    start_time: float | None
    end_time: float | None
    retrieval_status: Literal["MATCH_FOUND", "NO_MATCH", "REVIEW_REQUIRED", "SEARCH_ERROR"]
    evidence: list[Evidence] = Field(default_factory=list)
    suggested_verdict: Verdict | None = None
    explanation: str = ""
    assigned_team: str | None = None
    team_candidates: list[str] = Field(default_factory=list)
    review_status: Literal["PENDING_HUMAN_REVIEW"] = "PENDING_HUMAN_REVIEW"
    errors: list[str] = Field(default_factory=list)


class AnalysisResult(StrictModel):
    segment_id: str
    speaker: str
    status: Literal["COMPLETED", "PARTIAL", "ERROR"]
    claims: list[ClaimResult] = Field(default_factory=list)
    provider: str
    model: str
    analyzed_at: str
    transcription: str
    errors: list[str] = Field(default_factory=list)
