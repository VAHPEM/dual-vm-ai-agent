# -*- coding: utf-8 -*-
"""skills/ is the single source of the agent skills; skills/sync.py copies them out."""
import importlib.util
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

_spec = importlib.util.spec_from_file_location("skills_sync", SKILLS / "sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_every_skill_has_front_matter_naming_it():
    names = sync.skill_names(str(SKILLS))
    assert {"binary-analysis", "vuln-research"} <= set(names)
    for n in names:
        text = (SKILLS / n / "SKILL.md").read_text(encoding="utf-8")
        assert re.match(r"---\n.*?^description:", text, re.S | re.M), n


def test_no_skill_source_left_inside_the_package_or_scripts():
    assert not (ROOT / "labhand" / "playbook.py").exists()
    assert not (ROOT / "scripts" / "gen_skill.py").exists()
    hits = [p for p in ROOT.rglob("SKILL.md")
            if not {".git", "research", ".claude"} & set(p.relative_to(ROOT).parts)
            and p.parent.parent != SKILLS]
    assert hits == []


def test_binary_analysis_keeps_every_rule():
    text = (SKILLS / "binary-analysis" / "SKILL.md").read_text(encoding="utf-8")
    for r in range(1, 11):
        assert re.search(rf"^\s*R{r}\b", text, re.M), f"rule R{r} missing"


def _make_src(tmp_path, names=("alpha", "beta")):
    src = tmp_path / "src"
    for n in names:
        (src / n).mkdir(parents=True)
        (src / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: d\n---\nbody {n}\n")
    (src / "sync.py").write_text("# not a skill\n")
    return src


def test_sync_copies_checks_and_prunes_only_its_own(tmp_path):
    src = _make_src(tmp_path)
    (src / "alpha" / "ref.md").write_text("extra file\n")
    dest = tmp_path / "dest"
    (dest / "mine").mkdir(parents=True)                  # a skill the user put there
    (dest / "mine" / "SKILL.md").write_text("keep me")
    assert sync.drift(str(dest), str(src))               # missing before the first sync
    names, stale = sync.sync(str(dest), str(src))
    assert names == ["alpha", "beta"] and stale == []
    assert (dest / "alpha" / "ref.md").read_text() == "extra file\n"
    assert sync.drift(str(dest), str(src)) == []
    (dest / "beta" / "SKILL.md").write_text("hand edit")  # someone edited a copy
    assert any(line.startswith("differs:") for line in sync.drift(str(dest), str(src)))
    import shutil
    shutil.rmtree(src / "beta")                          # skill removed from the source
    names, stale = sync.sync(str(dest), str(src))
    assert stale == ["beta"] and not (dest / "beta").exists()
    assert (dest / "mine" / "SKILL.md").read_text() == "keep me"
    assert not [p for p in dest.iterdir() if p.name.endswith(".sync-tmp")]


def test_sync_refuses_to_clobber_a_foreign_skill_with_other_files(tmp_path):
    src = _make_src(tmp_path, ("alpha",))
    dest = tmp_path / "dest"
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha" / "notes.txt").write_text("not ours")
    with pytest.raises(sync.SyncError):
        sync.sync(str(dest), str(src))
    assert (dest / "alpha" / "notes.txt").exists()


def test_bad_front_matter_is_an_error(tmp_path):
    src = tmp_path / "src"
    (src / "alpha").mkdir(parents=True)
    (src / "alpha" / "SKILL.md").write_text("---\nname: other\n---\n")
    with pytest.raises(sync.SyncError):
        sync.skill_names(str(src))


def test_cli_check_exit_codes(tmp_path, capsys):
    dest = tmp_path / "d"
    assert sync.main(["--check", "--dest", str(dest)]) == 1
    assert sync.main(["--dest", str(dest)]) == 0
    assert sync.main(["--check", "--dest", str(dest)]) == 0
    assert os.path.isfile(dest / "binary-analysis" / "SKILL.md")


def test_committed_claude_skills_match_the_source():
    """.claude/skills/ is committed (a fresh clone has the project skills) and must equal
    skills/ - run `python skills/sync.py` after editing a skill."""
    assert sync.drift(str(ROOT / ".claude" / "skills"), str(SKILLS)) == []
