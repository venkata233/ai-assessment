# Session brownfield

- Provider: `mock` (local)
- Redactions before sending/storing (counts only): {"AWS_ACCESS_KEY": 1, "SECRET_VALUE": 2}
- Injection-like phrases flagged in the brief: none
- Audit chain head (`audit.jsonl`, verify with `python -m copilot verify`): `ca4c718f14f389d9033a1498ffa3af71d2db1fef2d7e4b1f284d689eaab876dd`

| Stage | Title | Engineer decision | Proposals | Rejections | Decision mode |
|---|---|---|---|---|---|
| 1 | Requirement understanding | accept | 1 | 0 | scripted |
| 2 | Task decomposition | accept | 1 | 0 | scripted |
| 3 | Architecture and decisions | accept | 1 | 0 | scripted |
| 4 | Implementation plan with AI | accept | 1 | 0 | scripted |
| 5 | Validation plan | accept | 1 | 0 | scripted |
| 6 | Risks and trade-offs | accept | 1 | 0 | scripted |
| 7 | Documentation and self-review | accept | 1 | 0 | scripted |

Decision mode `interactive` means a person answered at the gate; `scripted` means decisions were read from a file.
