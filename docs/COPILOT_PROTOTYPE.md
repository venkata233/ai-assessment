# Engineering Copilot prototype (Part A)

Status: **Phase 5 built and tested offline; not committed.** Code: [`copilot-prototype/`](../copilot-prototype). Examples: [`examples/`](../examples).

## 1. Read this first: what this is and is not

- It is a **human-in-the-loop workflow tool**: it walks an engineer through seven stages, proposes text at each one, and refuses to move on until the engineer accepts, modifies or rejects it.
- **Mock mode is the default and runs fully offline.** The mock provider is a **deterministic rule-based text generator, not an AI model**. It extracts requirement-like sentences, flags vague words, scans code with regexes, and fills templates. Where a real decision is needed it writes `DECISION REQUIRED` and never decides. The three examples therefore demonstrate the **engine, the gates, redaction and the audit log**, not model quality.
- The `openai` provider (any OpenAI-compatible `/chat/completions` endpoint) exists but has been **tested only against a local stub server**. It has **never been run against a real service** (no key was available). Treat it as unverified.
- Decisions in the examples are **scripted** (read from `decisions.json`) and recorded as `scripted`. They are not human decisions and are labelled that way in every audit log and summary.

## 2. Assumptions (please confirm)

The original assessment brief was not in the workspace. The only specification found is task T23 in [PHASE1_REQUIREMENTS_AND_PLAN.md](PHASE1_REQUIREMENTS_AND_PLAN.md): *"CLI/web UI, 7-stage engine, providers, redaction, mock mode, 3 examples; runs offline in mock mode; engineer designs HITL gates and redaction rules."* Therefore:

| Assumption | Why it matters |
|---|---|
| The **seven stages are my interpretation**, taken from this repository's own process: 1 Requirement understanding, 2 Task decomposition, 3 Architecture and decisions, 4 Implementation plan with AI, 5 Validation plan, 6 Risks and trade-offs, 7 Documentation and self-review. | If the real brief names other stages, `stages.py` and `providers/mock.py` need changing. |
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
- Each session is a folder: `00_brief.md`, `01_requirements.md` ... `07_review.md`, `SUMMARY.md`, `session.json`, `audit.jsonl`, `prompts/`.

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
| [greenfield](../examples/greenfield) | A clear brief with numbers: only 3 questions (the unstated non-functionals); a `modify` decision that appends an engineer constraint. |
| [ambiguous](../examples/ambiguous) | "Scalable, fast, secure, easy, etc.": 11 questions; the first proposal is **rejected** with a reason (G4), then accepted with answers (G3). |
| [brownfield](../examples/brownfield) | Legacy code with a check-then-insert race, string-built SQL, `random`, `md5` and three fake credentials: the scanner flags them, redaction removes the values (AWS docs example key and made-up passwords), and the engineer answers the code-finding questions. |

Regenerate: see the command in each example README.

## 9. Verification status

| Item | Status |
|---|---|
| Unit/integration tests (`copilot-prototype/tests`) | **123 passed, 1 skipped** on Windows, Python 3.11.4. Skipped: the symlink-handling test needs privileges, so that path is **unverified here**. |
| ruff, ruff format, mypy (strict, 17 files) | Clean |
| Examples reproducible | Verified by `tests/test_examples.py` |
| Real model output | **Not verified.** Mock mode contains no AI. |
| `openai` provider against a real service | **Not verified** (local stub server only) |
| Web UI in a real browser | **Not verified** |
| Coverage measurement, mutation testing, security review of this tool | **Not done** |
| Any Linux/macOS run | **Not done** |

## 10. Risks specific to this tool

See [RISKS.md](RISKS.md) A09 (prompt injection). In this tool the brief and brownfield files are untrusted: with a real model, text inside them could try to steer the output. Mitigations present: the engineer must review every stage (G1-G4), injection-like phrases in the brief are flagged in the summary and audit log, and nothing is executed. The flagging is a keyword heuristic and will miss rephrased attacks; it is a hint, not a defence. Other limits: redaction misses unlabelled secrets (section 5); a model may still produce plausible but wrong stage text, which is why acceptance is a human step and why the examples must not be read as evidence of AI quality.
