# Session brownfield

- Provider: `mock` (local)
- Redactions before sending/storing (counts only): {"AWS_ACCESS_KEY": 1, "SECRET_VALUE": 2}
- Injection-like phrases flagged in the brief: none
- Audit chain head (`audit.jsonl`, verify with `python -m copilot verify`): `e328f251247b678cbc9cf7bc2cc730f75ae5521a6062f675f38a7ea6f127f925`

| Stage | Title | Engineer decision | Proposals | Rejections | Decision mode |
|---|---|---|---|---|---|
| 1 | Requirement Understanding | accept | 1 | 0 | scripted |
| 2 | Task Decomposition | accept | 1 | 0 | scripted |
| 3 | AI-Assisted Development | accept | 1 | 0 | scripted |
| 4 | Output Generation | accept | 1 | 0 | scripted |
| 5 | QA | accept | 1 | 0 | scripted |
| 6 | Risk Awareness | accept | 1 | 0 | scripted |
| 7 | Engineering Summary | accept | 1 | 0 | scripted |

Decision mode `interactive` means a person answered at the gate; `scripted` means decisions were read from a file.
