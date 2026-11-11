"""Every stage is usable without the stages above it (docs/constitution.md, 3).

Text: a file of a stage names no component of a higher stage. Code: a skill imports only the standard
library, PyYAML and its own files, and no string in it names the folder of another skill.

    python3 tests/run.py --only stages
"""

import ast
import re
import sys
import unittest

from test_structure import PLUGIN, SKILLS, files

# stage -> (where its files are, the names a lower stage does not use)
STAGES = {
    1: (("mcp-servers/", "skills/coverity-setup/", "skills/coverity-triage/", "skills/coverity-apply/"), ()),
    2: (("agents/triage-worker.md", "com.github.copilot/agents/triage-worker.agent.md"), ("triage-worker",)),
    3: (("agents/", "com.github.copilot/agents/"), ("triage-orchestrator", "triage-reviewer", "triage-integrator")),
    4: (("skills/coverity-gui/",), ("coverity-gui", "gui.py", "decisions.json", "画面")),
}
# coverity-apply reads what the person chose on the screen.
ALLOWED = {"skills/coverity-apply/": ("decisions.json", "画面")}
# Inside stage 1: finish of triage.py gives the plan line of coverity-apply, which shows the one table.
HANDS_ON = {"skills/coverity-triage/scripts/triage.py": ("coverity-apply",)}
CODE = (".py", ".js", ".html", ".css")


def stage_of(name: str):
    return next((stage for stage, (places, _) in STAGES.items() if name.startswith(places)), None)


def read(path) -> str:
    return (PLUGIN / path).read_text(encoding="utf-8", errors="replace")


class Stages(unittest.TestCase):
    def test_a_file_names_no_component_of_a_higher_stage(self):
        names = [path.as_posix() for path in files()]
        self.assertEqual({stage_of(name) for name in names} - {None}, set(STAGES), "every stage has files")
        found = []
        for name in names:
            above = [word for stage, (_, words) in STAGES.items() if stage > (stage_of(name) or len(STAGES)) for word in words]
            allowed = next((words for place, words in ALLOWED.items() if name.startswith(place)), ())
            found += [f"{name}: {word}" for word in above if word not in allowed and word in read(name)]
        self.assertEqual(found, [])

    def test_a_skill_uses_no_file_of_another_skill(self):
        every, found = files(), []
        for path in every:
            if path.parts[0] != "skills" or path.suffix not in CODE:
                continue
            name, skill, strings = path.as_posix(), path.parts[1], [read(path)]
            if path.suffix == ".py":
                nodes = list(ast.walk(ast.parse(strings[0])))
                strings = [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]
                imported = {alias.name for node in nodes if isinstance(node, ast.Import) for alias in node.names}
                imported |= {node.module for node in nodes if isinstance(node, ast.ImportFrom) and not node.level}
                own = {other.stem for other in every if other.parts[:2] == path.parts[:2]}
                found += [f"{name}: import {module}" for module in sorted({module.split(".")[0] for module in imported}
                                                                          - set(sys.stdlib_module_names) - own - {"yaml"})]
            # the folder name alone: not .coverity-triage, coverity-triage-out or /coverity-triage-kit:coverity-setup
            others = re.compile(rf"(?<![\w.:-])(?:{'|'.join(other for other in SKILLS if other != skill)})(?![\w-])")
            found += [f"{name}: {match}" for text in strings for match in others.findall(text)
                      if match not in HANDS_ON.get(name, ())]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
