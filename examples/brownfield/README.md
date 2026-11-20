# Example: brownfield

`legacy/app.py` is a small, deliberately flawed file: check-then-insert race, string-built SQL, `random` for codes, MD5, and three **fake** credentials (made-up password, the AWS documentation example key id and secret). **Mock provider** (rule-based, offline, not an AI) and **scripted** decisions (`decisions.json`).

Regenerate (from `copilot-prototype/`, PowerShell):

```
$env:PYTHONPATH="src"
python -m copilot run --brief ..\examples\brownfield\brief.md --context ..\examples\brownfield\legacy --decisions ..\examples\brownfield\decisions.json --deterministic --session-id brownfield --out ..\examples\brownfield\output
python -m copilot verify brownfield --out ..\examples\brownfield\output
```

What to look at: `output/brownfield/01_requirements.md` (a table of rule-based findings, which are **candidates, not proof**; credentials appear only as `[REDACTED:...]`), `04_output_generation.md` (a fix sketch per distinct finding rule, plus skipped test skeletons), `SUMMARY.md` (redaction counts), `audit.jsonl`. A test checks that none of the fake secret values appear anywhere under `output/`. The scanner caps the number of code-finding questions, so the second SQL finding (line 23) is listed in the table but has no question.
