# Engineering Copilot prototype (Part A)

Status: **Phase 5 built and tested offline; committed in `47c847d`; awaiting the engineer's review.** Code: [`copilot-prototype/`](../copilot-prototype). Examples: [`examples/`](../examples).

## 1. Read this first: what this is and is not

- It is a **human-in-the-loop workflow tool**: it walks an engineer through seven stages, proposes text at each one, and refuses to move on until the engineer accepts, modifies or rejects it.
- **Mock mode is the default and runs fully offline.** The mock provider is a **deterministic rule-based text generator, not an AI model**. It extracts requirement-like sentences, flags vague words, scans code with regexes, and fills templates. Where a real decision is needed it writes `DECISION REQUIRED` and never decides. The three examples therefore demonstrate the **engine, the gates, redaction and the audit log**, not model quality.
- The `openai` provider (any OpenAI-compatible `/chat/completions` endpoint) exists but has been **tested only against a local stub server**. It has **never been run against a real service** (no key was available). Treat it as unverified.
- Decisions in the examples are **scripted** (read from `decisions.json`) and recorded as `scripted`. They are not human decisions and are labelled that way in every audit log and summary.

## 2. The seven stages (the engineer's required names)

The stage names are the engineer's required list, supplied in the Phase 6 review. The original brief was not in the workspace when Part A was first built, so the first version used AI-derived names ([PHASE1_REQUIREMENTS_AND_PLAN.md](PHASE1_REQUIREMENTS_AND_PLAN.md) T23 only said *"7-stage engine"*); that version is superseded. **Architecture and design decisions are not a stage of their own**: they are the design subtask of Task Decomposition. Each stage's required headings drive gate G2.

| # | Stage (exact name) | Artifact | What the stage produces in mock mode | Required headings |
|---|---|---|---|---|
| 1 | Requirement Understanding | `01_requirements.md` | Interpretation, ambiguities as assumptions (A-n) and open questions (Q-n), non-functional coverage, edge cases, acceptance criteria; for brownfield, rule-based code-scan findings | Interpretation; Ambiguities and assumptions; Non-functional requirements; Edge cases; Acceptance criteria; Open questions |
| 2 | Task Decomposition | `02_decomposition.md` | A task table (including a "Decide the architecture" design task), dependencies, the AI/engineer split, and the **design subtask**: components, options and trade-offs for each decision (each ends in `DECISION REQUIRED`), interfaces and data | Tasks; Dependencies; AI assistance and engineer responsibility; Architecture and design decisions |
| 3 | AI-Assisted Development | `03_ai_development.md` | A prompt for each build or fix task, **built from the engineer-accepted task table, acceptance criteria and design decisions** (an edited table changes the prompts); expected failure modes tied to each task; an **iteration log** (AI suggestion v1, issue the engineer found, refined prompt, outcome) left as `TBD` for the engineer; a review checklist. The tool prepares and records AI-assisted development; it does not write the code | Task prompts; Expected failure modes; Iteration log; Review checklist |
| 4 | Output Generation | `04_output_generation.md` | **Draft deliverables generated from the accepted stages:** a manifest (task, deliverable, check before use, status `DRAFT`); one **skipped** acceptance-test skeleton per acceptance criterion; for brownfield code, a fix sketch per distinct finding rule (unique constraint with `ON CONFLICT`, bound parameters, `secrets`, SHA-256, environment variables, and so on); a draft change summary. Rule-based templates, untested | Deliverables; Draft artifacts; Output review checklist |
| 5 | QA | `05_qa.md` | Test strategy per acceptance criterion, security checks by component, traceability, and **how each generated output is verified** | Test strategy; Security checks; Traceability; Output verification |
| 6 | Risk Awareness | `06_risk_awareness.md` | Risk register (likelihood and impact left for the engineer to rate), AI-specific risks including draft outputs treated as finished work, mitigations and accepted trade-offs | Risk register; AI-specific risks; Mitigations and accepted trade-offs |
| 7 | Engineering Summary | `07_summary.md` | Requirement-to-task traceability (mapped from the accepted task table), unverified claims (placeholders, assumptions, skipped skeletons), open items, hand-off summary | Traceability matrix; Unverified claims; Open items; Hand-off summary |

**What changed from the first version (Phase 6 correction).** Stages 1 and 2 kept their content. The former stage 3 "Architecture and decisions" was removed as a stage and moved into stage 2 as the design subtask. The former stage 4 "Implementation plan with AI" became stage 3: its prompts, failure modes and review checklist stayed, prompts are now built from the *accepted* tasks, failure modes are tied to tasks (matched on whole words), and the iteration log was added. Stage 4 Output Generation is new. Validation plan, Risks and trade-offs, and Documentation and self-review became QA (plus Output verification), Risk Awareness and Engineering Summary. The three examples were regenerated, so their audit hashes changed; the earlier outputs exist only in git history (`47c847d`). **Honest limits:** in mock mode nothing here is written by a model; stage 3 prepares and records the engineer's AI-assisted work, stage 4 fills templates, and every generated draft is untested and marked as such.

## 2a. Other assumptions (please confirm)

| Assumption | Why it matters |
|---|---|
| The three examples are *greenfield*, *brownfield* and *ambiguous* (names taken from the layout in the plan). | Content of each brief is my own. |
| Stdlib-only, no web framework, no third-party runtime dependency. | Keeps it runnable offline and cheap on the laptop; the UI is deliberately minimal. |

## 3. Architecture

```
cli.py / web.py  --->  engine.py (state machine + gates G1-G6)  --->  providers/{mock,openai_compat}.py
                          |        |                |
                    redaction.py  audit.py       store.py (session.json, stage files, prompts/)
                    context.py (brownfield files)  analyzers.py (brief + code heuristics)
```

- `engine.py` owns every rule; the CLI and the web UI only collect decisions, so no front end can bypass a gate.
- Each session is a folder: `00_brief.md`, `01_requirements.md`, `02_decomposition.md`, `03_ai_development.md`, `04_output_generation.md`, `05_qa.md`, `06_risk_awareness.md`, `07_summary.md`, `SUMMARY.md`, `session.json`, `audit.jsonl`, `prompts/`.

## 4. Human-in-the-loop gates (enforced in `engine.py`)

| Gate | Rule |
|---|---|
| G1 | A stage cannot be skipped: stage N+1 starts only after stage N is accepted or modified. |
| G2 | A proposal missing a required `##` section cannot be accepted as is; the engineer must modify it. |
| G3 | Every open question `- Q<n>:` needs an engineer answer or the explicit word `ASSUME` before the stage closes. |
| G4 | A rejection needs a reason (kept, redacted, and fed to the next attempt). After 3 rejections (`--max-rejections`) the tool stops regenerating and the engineer must write the text. |
| G5 | Nothing goes to an external provider without an outbound preview, showing cumulative redaction counts, that the engineer confirms. |
| G6 | Non-interactive decision modes (`scripted`, `auto`) are refused for external providers. |

The engine, not the caller, sets the recorded decision mode (`interactive`, `scripted`, `auto`).

## 5. Redaction rules

Applied to the brief, brownfield context, rejection reasons and engineer text **before** anything is sent to a provider or written to disk. Replacements are stable placeholders such as `[REDACTED:SECRET_VALUE:1]`; reports contain **counts only**, never values.

| Detector | Catches |
|---|---|
| PRIVATE_KEY | PEM private-key blocks (bounded length) |
| AWS_ACCESS_KEY | `AKIA...` / `ASIA...` ids |
| GITHUB_TOKEN, API_KEY, SLACK_TOKEN, JWT | Well-known token formats |
| BEARER_TOKEN | `Bearer <token>` |
| URL_CREDENTIALS | `scheme://user:password@host` |
| SECRET_VALUE | `name = value` / `name: value` where the name contains password, passwd, pass, secret, token, api key, access key, private key, client secret (so `ADMIN_PASSWORD`, `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN` are caught); value of 4+ characters |
| EMAIL, IPV4 | Personal data and non-loopback addresses |

Known limits (tested or documented, not hidden):

- **Pattern-based.** A bare secret with no label and no known format (for example a lone random string) is **not** detected; a test documents this.
- Placeholder numbering restarts if a session is resumed in a new process.
- Name matching allows at most 64 characters before and after the keyword, so very long identifiers can be missed. This cap exists because an unbounded pattern was measured to take 14 s on adversarial input.
- The brownfield scanner reports `HARDCODED_SECRET` for lines the engine has already redacted, so a redacted secret is still flagged without its value.

## 6. Audit log

`audit.jsonl` is a SHA-256 hash chain (`python -m copilot verify <session>`). It records prompt/response hashes, redaction counts, decisions and their mode, and **refuses to store secret-like data**. It is **tamper-evident, not tamper-proof**: editing or deleting a middle entry is detected; truncating the tail is not.

## 7. Usage

All commands run from `copilot-prototype/` with `PYTHONPATH=src` (no install step was tested). No network is used in mock mode.

```
python -m copilot run --brief brief.md                      # interactive, mock
python -m copilot run --brief brief.md --context ./legacy   # brownfield: reads code files, redacts, scans
python -m copilot run --brief brief.md --decisions d.json --deterministic --session-id demo --out ./out   # scripted
python -m copilot resume <session>
python -m copilot verify <session>
python -m copilot redact file.txt                           # shows what would be redacted; sends nothing
python -m copilot web --port 8765                           # http://127.0.0.1:8765 only
```

External provider (unverified): set `COPILOT_BASE_URL`, `COPILOT_MODEL` and, for hosted services, `COPILOT_API_KEY`, then add `--provider openai`. https is required except for loopback hosts, redirects are not followed, responses are capped at 1 MB, and error messages never include the key or response body.

### Web UI

Minimal, stdlib `http.server`, **127.0.0.1 only**. Defences: exact `Host` check (DNS rebinding), per-session HMAC CSRF token plus `Origin` check, 64 KB body limit, all output HTML-escaped, strict CSP, artifact downloads allow-listed. Brownfield `--context` is CLI only. An oversized body is rejected before it is read, so some Windows clients see a connection reset instead of an HTTP 413. The UI was **tested with an in-process HTTP client, not driven in a browser**.

## 8. Examples

Each folder has `brief.md`, `decisions.json`, `README.md` and `output/<name>/` (generated; reproduced byte for byte by a test).

| Example | What it shows |
|---|---|
| [greenfield](../examples/greenfield) | A clear brief with numbers: only 3 questions (the unstated non-functionals); a `modify` decision at stage 2 that appends an engineer constraint to the design subtask. |
| [ambiguous](../examples/ambiguous) | "Scalable, fast, secure, easy, etc.": 11 questions; the first proposal is **rejected** with a reason (G4), then accepted with answers (G3). |
| [brownfield](../examples/brownfield) | Legacy code with a check-then-insert race, string-built SQL, `random`, `md5` and three fake credentials: the scanner flags them, redaction removes the values (AWS docs example key and made-up passwords), and the engineer answers the code-finding questions. |

Regenerate: see the command in each example README.

## 9. Verification status

| Item | Status |
|---|---|
| Unit/integration tests (`copilot-prototype/tests`) | 132 tests collected. After the Phase 6 stage correction these were re-run: `test_providers` (31), `test_engine`, `test_cli`, `test_web` (47 together) and `test_examples` (4): **all pass**. `test_analyzers`, `test_audit`, `test_context` and `test_redaction` (50) were not touched and **not re-run** in that step; the last full run (before the correction) was 123 passed, 1 skipped on Windows, Python 3.11.4. The skipped test (symlink handling) needs privileges, so that path is **unverified here**. |
| ruff, ruff format, mypy (strict, 17 files) | Clean |
| Examples reproducible | Verified by `tests/test_examples.py` |
| Real model output | **Not verified.** Mock mode contains no AI. |
| `openai` provider against a real service | **Not verified** (local stub server only) |
| Web UI in a real browser | **Not verified** |
| Coverage measurement, mutation testing, security review of this tool | **Not done** |
| Any Linux/macOS run | **Not done** |

## 10. Risks specific to this tool

See [RISKS.md](RISKS.md) A09 (prompt injection). In this tool the brief and brownfield files are untrusted: with a real model, text inside them could try to steer the output. Mitigations present: the engineer must review every stage (G1-G4), injection-like phrases in the brief are flagged in the summary and audit log, and nothing is executed. The flagging is a keyword heuristic and will miss rephrased attacks; it is a hint, not a defence. Other limits: redaction misses unlabelled secrets (section 5); a model may still produce plausible but wrong stage text, which is why acceptance is a human step and why the examples must not be read as evidence of AI quality.
