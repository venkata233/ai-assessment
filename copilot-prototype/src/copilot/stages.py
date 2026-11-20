"""The seven assessment stages and what each output must contain.

The stage names are the engineer's required list (Phase 6 review): Requirement Understanding, Task Decomposition,
AI-Assisted Development, Output Generation, QA, Risk Awareness, Engineering Summary. Architecture and design decisions are
not a stage of their own: they are a required subtask section of Task Decomposition (stage 2).
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
        "Requirement Understanding",
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
        "Task Decomposition",
        "Break the work into tasks with dependencies and an explicit split between AI assistance and engineer ownership; "
        "include the architecture and design decisions as a subtask.",
        (
            "Tasks",
            "Dependencies",
            "AI assistance and engineer responsibility",
            "Architecture and design decisions",
        ),
    ),
    StageSpec(
        3,
        "ai_development",
        "AI-Assisted Development",
        "Drive the build task by task with an AI assistant: a prompt for each accepted task, the failure modes to expect, "
        "an iteration log (AI suggestion, issue found, refined prompt, decision) and the review the engineer applies to AI output.",
        ("Task prompts", "Expected failure modes", "Iteration log", "Review checklist"),
    ),
    StageSpec(
        4,
        "output_generation",
        "Output Generation",
        "Produce the draft deliverables from the accepted stages: a deliverables manifest, acceptance-test skeletons, "
        "fix sketches for flagged code, a change summary, and the checks that must pass before any draft is used.",
        ("Deliverables", "Draft artifacts", "Output review checklist"),
    ),
    StageSpec(
        5,
        "qa",
        "QA",
        "Define tests, security checks, traceability from acceptance criteria to evidence, and how the generated outputs are verified.",
        ("Test strategy", "Security checks", "Traceability", "Output verification"),
    ),
    StageSpec(
        6,
        "risk_awareness",
        "Risk Awareness",
        "Record product and AI-specific risks with likelihood, impact, and mitigation.",
        ("Risk register", "AI-specific risks", "Mitigations and accepted trade-offs"),
    ),
    StageSpec(
        7,
        "summary",
        "Engineering Summary",
        "Check completeness, list unverified claims and open items, and write the hand-off summary.",
        ("Traceability matrix", "Unverified claims", "Open items", "Hand-off summary"),
    ),
)

STAGE_BY_NUMBER = {s.number: s for s in STAGES}
FIRST_STAGE = STAGES[0].number
LAST_STAGE = STAGES[-1].number
