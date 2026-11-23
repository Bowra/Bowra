"""`skill_invoke` names an entry script either from scripts/ or by its path from the Skill root."""

from bowhead_skill.tools import entry_script_name


def test_entry_script_name_rule():

    entries = ["build.py", "tools/run.py", "scripts/odd.py"]
    assert entry_script_name("build.py", entries) == "build.py"
    assert entry_script_name("scripts/tools/run.py", entries) == "tools/run.py"
    assert entry_script_name("././scripts/build.py", entries) == "build.py"
    # An exact entry name wins over the Skill-root reading (an entry inside scripts/scripts/).
    assert entry_script_name("scripts/odd.py", entries) == "scripts/odd.py"
    # Nothing outside scripts/ becomes an entry; unknown names are left for the refusal to list the entries.
    assert entry_script_name("assets/build.py", entries) == "assets/build.py"
    assert entry_script_name("scripts/", entries) == "scripts/"
