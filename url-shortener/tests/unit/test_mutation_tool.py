"""Guards for tools/mutation_check.py that never run a mutant: patterns must match the source; skipped is not killed."""

import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def _load_tool():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("mutation_check", PROJECT / "tools" / "mutation_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mutation_check"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_every_mutant_pattern_still_matches_the_current_source() -> None:
    stale = []
    for m in tool.MUTATIONS:
        source = (PROJECT / m.file).read_text(encoding="utf-8")
        if tool.apply_mutation(m, source) is None:
            stale.append(m.name)
        for test in m.tests:
            assert (PROJECT / test.split("::")[0]).exists(), (m.name, test)
    assert stale == [], f"these mutants would be SKIPPED because the source changed: {stale}"


def test_mutants_are_distinct_so_one_defect_is_not_counted_twice() -> None:
    keys = [(m.file, m.old, m.new) for m in tool.MUTATIONS]
    assert len(set(keys)) == len(keys) == 27


def test_a_mutant_that_cannot_be_applied_yields_none_not_a_mutation() -> None:
    mutation = tool.Mutation("x", "f.py", "old", "new", ["tests/unit"])
    assert tool.apply_mutation(mutation, "nothing to see") is None
    assert tool.apply_mutation(tool.Mutation("x", "f.py", "same", "same", []), "same") is None
    needs_second = tool.Mutation("x", "f.py", "a", "b", [], also=("missing", "z"))
    assert tool.apply_mutation(needs_second, "a a") is None
    assert tool.apply_mutation(mutation, "old old") == "new old"  # only the first occurrence


def test_skipped_mutants_are_never_counted_as_killed() -> None:
    text, code = tool.summarise(["a"], ["b"], ["c", "d"])
    assert text.startswith("1/2 applied mutants killed (50%)") and "['c', 'd']" in text and code == 1
    text, code = tool.summarise(["a", "b"], [], ["c"])
    assert text.startswith("2/2 applied mutants killed (100%)") and code == 3  # incomplete is not success
    assert (
        tool.summarise([], [], ["c"])[0].startswith("0/0 applied mutants killed")
        and tool.summarise([], [], ["c"])[1] == 3
    )
    assert tool.summarise(["a"], [], [])[1] == 0
