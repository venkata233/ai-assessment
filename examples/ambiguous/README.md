# Example: ambiguous

The brief is deliberately vague ("scalable", "fast", "secure", "easy", "etc."). **Mock provider** (rule-based, offline, not an AI) and **scripted** decisions (`decisions.json`), so this shows the workflow, gates and audit log, not model quality.

Regenerate (from `copilot-prototype/`, PowerShell):

```
$env:PYTHONPATH="src"
python -m copilot run --brief ..\examples\ambiguous\brief.md --decisions ..\examples\ambiguous\decisions.json --deterministic --session-id ambiguous --out ..\examples\ambiguous\output
python -m copilot verify ambiguous --out ..\examples\ambiguous\output
```

What to look at: `output/ambiguous/01_requirements.md` (11 open questions: 5 vague words, 6 unstated non-functionals), the first stage-1 proposal being **rejected** with a reason (gate G4, see `audit.jsonl`, `rejections: 1`) and then accepted with answers (gate G3; some answers are `ASSUME`). The numbers in the answers are invented for the example.
