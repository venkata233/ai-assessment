"""Session state. Plain dataclasses with explicit (de)serialisation so the on-disk format is stable and reviewable."""

from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["accept", "modify", "reject"]
Status = Literal["awaiting_proposal", "awaiting_decision", "complete", "aborted"]


@dataclass
class Proposal:
    stage: int
    revision: int
    text: str
    provider: str
    model: str
    prompt_sha256: str
    response_sha256: str
    redaction_in: dict[str, int]  # categories redacted from what we sent (counts only)
    redaction_out: dict[str, int]  # categories found (and redacted) in the provider's answer
    missing_sections: list[str]
    open_questions: dict[str, str]  # id -> question text
    created_at: str


@dataclass
class Decision:
    stage: int
    action: Action
    notes: str = ""
    answers: dict[str, str] = field(default_factory=dict)
    text: str | None = None  # full replacement text (modify)
    append: str | None = None  # text appended to the proposal (modify)
    mode: str = "interactive"  # interactive | scripted | auto
    decided_at: str = ""


@dataclass
class StageRecord:
    stage: int
    proposals: list[Proposal] = field(default_factory=list)
    decision: Decision | None = None
    rejections: int = 0
    feedback: list[str] = field(
        default_factory=list
    )  # engineer reasons for rejections, redacted; fed to the next attempt
    final_text: str | None = None


@dataclass
class Session:
    id: str
    created_at: str
    provider: str
    external_provider: bool
    brief: str  # redacted
    context_files: list[dict[str, Any]]  # path, bytes, truncated (no contents)
    context_skipped: list[dict[str, str]]
    redaction_summary: dict[str, int]
    injection_flags: list[str]
    deterministic: bool
    current_stage: int = 1
    status: Status = "awaiting_proposal"
    records: dict[int, StageRecord] = field(default_factory=dict)

    def record(self, stage: int) -> StageRecord:
        return self.records.setdefault(stage, StageRecord(stage))


def to_dict(session: Session) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(session)
    data["records"] = {str(k): v for k, v in data["records"].items()}
    return data


def from_dict(data: dict[str, Any]) -> Session:
    records: dict[int, StageRecord] = {}
    for key, raw in data["records"].items():
        decision = Decision(**raw["decision"]) if raw.get("decision") else None
        records[int(key)] = StageRecord(
            stage=raw["stage"],
            proposals=[Proposal(**p) for p in raw["proposals"]],
            decision=decision,
            rejections=raw["rejections"],
            feedback=list(raw.get("feedback", [])),
            final_text=raw["final_text"],
        )
    return Session(**{**data, "records": records})
