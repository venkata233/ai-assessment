"""The seven stages and what each output must contain.

ASSUMPTION (recorded in docs/COPILOT_PROTOTYPE.md): the original assessment brief was not available in the
workspace, so the seven stages are derived from the delivery process this repository itself followed (requirements,
decomposition, design, implementation with AI, validation, risks, documentation and self-review).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StageSpec:
    number: int
    key: str
    title: str
    purpose: str
    # Level-2 Markdown headings a proposal must contain; a proposal missing one cannot be accepted as-is.
    required_sections: tuple[str, ...]


STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        1,
        "requirements",
        "Requirement understanding",
        "Interpret the brief, surface ambiguities, label assumptions, state non-functional needs and acceptance criteria.",
        (
            "Interpretation",
            "Ambiguities and assumptions",
            "Non-functional requirements",
            "Edge cases",
            "Acceptance criteria",
            "Open questions",
        ),
    ),
    StageSpec(
        2,
        "decomposition",
        "Task decomposition",
        "Break the work into tasks with dependencies and an explicit split between AI assistance and engineer ownership.",
        ("Tasks", "Dependencies", "AI assistance and engineer responsibility"),
    ),
    StageSpec(
        3,
        "design",
        "Architecture and decisions",
        "Propose components and the decisions that need the engineer, each with options and trade-offs.",
        ("Components", "Decisions required", "Interfaces and data", "Trade-offs"),
    ),
    StageSpec(
        4,
        "implementation",
        "Implementation plan with AI",
        "Plan how AI is used per task, which failure modes to expect, and how each output is reviewed.",
        ("Prompts", "Expected failure modes", "Review checklist"),
    ),
    StageSpec(
        5,
        "validation",
        "Validation plan",
        "Define tests, security checks, and traceability from acceptance criteria to evidence.",
        ("Test strategy", "Security checks", "Traceability"),
    ),
    StageSpec(
        6,
        "risks",
        "Risks and trade-offs",
        "Record product and AI-specific risks with likelihood, impact, and mitigation.",
        ("Risk register", "AI-specific risks", "Mitigations and accepted trade-offs"),
    ),
    StageSpec(
        7,
        "review",
        "Documentation and self-review",
        "Check completeness, list unverified claims and open items, and write the hand-off summary.",
        ("Traceability matrix", "Unverified claims", "Open items", "Hand-off summary"),
    ),
)

STAGE_BY_NUMBER = {s.number: s for s in STAGES}
FIRST_STAGE = STAGES[0].number
LAST_STAGE = STAGES[-1].number
