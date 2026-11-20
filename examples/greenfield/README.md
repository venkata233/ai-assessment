# Example: greenfield

A clear brief with numbers. **Mock provider** (rule-based, offline, not an AI) and **scripted** decisions (`decisions.json`), so this shows the workflow, gates, redaction and audit log, not model quality.

Regenerate (from `copilot-prototype/`, PowerShell):

```
$env:PYTHONPATH="src"
python -m copilot run --brief ..\examples\greenfield\brief.md --decisions ..\examples\greenfield\decisions.json --deterministic --session-id greenfield --out ..\examples\greenfield\output
python -m copilot verify greenfield --out ..\examples\greenfield\output
```

What to look at: `output/greenfield/01_requirements.md` (only 3 open questions, for the non-functionals the brief does not state), `03_design.md` (engineer-appended constraint from a `modify` decision), `audit.jsonl`, `SUMMARY.md`.
