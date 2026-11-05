"""Checks the code checker's reading of modules, imports and parts, on small packages."""

from __future__ import annotations

from pathlib import Path

from ratchet.architecture import check_architecture, find_modules, read_imports
from ratchet.project import Config, Project
from ratchet.target_design import TargetDesign, read_target_design

PARTS = """
[parts]
core = "app.core"
cli = "app.cli"

[may_use]
core = []
cli = ["core"]
"""


def make_design(extra: str = "") -> TargetDesign:
    """Returns the design of PARTS with extra TOML added to the block."""
    design = read_target_design(f"```toml\n{PARTS}{extra}\n```\n")
    assert design is not None
    return design


def make_project(root: Path, files: dict[str, str]) -> Project:
    """Writes the files under root and returns a project whose editable files are under src."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = Config(test_command=["pytest"], editable=["src/**"])
    return Project(root=root, config=config, rules="rules")


def package(*names: str) -> dict[str, str]:
    """Returns empty __init__.py files for app and each subpackage given."""
    files = {"src/app/__init__.py": ""}
    for name in names:
        files[f"src/app/{name}/__init__.py"] = ""
    return files


def test_modules_are_named_from_their_packages(tmp_path: Path) -> None:
    """Proves the src layout, packages and tests are handled when naming and scoping files."""
    project = make_project(tmp_path, {
        **package("core"),
        "src/app/core/model.py": "",
        "src/app/script.py": "",
        "src/loose.py": "",
        "src/test_loose.py": "",
        "tests/test_other.py": "",
        "build/outside.py": "",
    })

    modules = find_modules(project, None)

    assert {(module.name, module.path, module.is_package) for module in modules} == {
        ("app", "src/app/__init__.py", True),
        ("app.core", "src/app/core/__init__.py", True),
        ("app.core.model", "src/app/core/model.py", False),
        ("app.script", "src/app/script.py", False),
        ("loose", "src/loose.py", False),
    }


def test_a_part_brings_in_files_outside_editable_and_skip_removes_them(tmp_path: Path) -> None:
    """Proves modules of a part are scanned although not editable, and skipped ones are not."""
    project = make_project(tmp_path, {
        "lib/__init__.py": "",
        "lib/shared.py": "",
        "lib/generated/__init__.py": "",
        "lib/generated/stub.py": "",
        "src/app/__init__.py": "",
    })
    design = read_target_design(
        '```toml\n[parts]\nlib = "lib"\n[may_use]\nlib = []\n'
        '[skip]\nmodules = ["lib.generated"]\n```\n'
    )

    names = {module.name for module in find_modules(project, design)}

    assert names == {"lib", "lib.shared", "app"}


def test_imports_are_resolved_and_filtered(tmp_path: Path) -> None:
    """Proves relative imports resolve, TYPE_CHECKING is skipped, and function imports are hidden."""
    project = make_project(tmp_path, {
        **package("core"),
        "src/app/core/model.py": "VALUE = 1\n",
        "src/app/core/rules.py": "",
        "src/app/core/use.py": (
            "from __future__ import annotations\n"
            "import os.path\n"
            "from typing import TYPE_CHECKING\n"
            "from . import model\n"
            "from .rules import check\n"
            "from .. import core\n"
            "from app.core.model import VALUE\n"
            "if TYPE_CHECKING:\n"
            "    from app.cli import console\n"
            "else:\n"
            "    import app.core.rules\n"
            "\n"
            "\n"
            "def run():\n"
            "    import app.script\n"
        ),
        "src/app/script.py": "",
    })
    modules = find_modules(project, None)
    known = {module.name for module in modules}
    use = next(module for module in modules if module.name == "app.core.use")

    imports = read_imports(tmp_path, use, known)

    assert [(item.target, item.line, item.hidden) for item in imports] == [
        ("__future__", 1, False),
        ("os.path", 2, False),
        ("typing", 3, False),
        ("app.core.model", 4, False),
        ("app.core.rules", 5, False),
        ("app.core", 6, False),
        ("app.core.model", 7, False),
        ("app.core.rules", 11, False),
        ("app.script", 15, True),
    ]
    assert imports[3].text == "from . import model"


def test_a_file_that_does_not_parse_gives_no_imports(tmp_path: Path) -> None:
    """Proves a broken file is passed over and does not stop the check."""
    project = make_project(tmp_path, {"src/app/__init__.py": "", "src/app/bad.py": "def (:\n"})
    modules = find_modules(project, None)

    assert check_architecture(project, None) == []
    assert read_imports(tmp_path, modules[1], {"app", "app.bad"}) == []


def test_forbidden_imports_are_one_stated_finding_per_pair(tmp_path: Path) -> None:
    """Proves forbidden imports group by pair of parts and cite each import line."""
    project = make_project(tmp_path, {
        **package("core", "cli"),
        "src/app/cli/console.py": "",
        "src/app/core/a.py": "from app.cli import console\n",
        "src/app/core/b.py": "import os\n\n\ndef f():\n    from app.cli.console import x\n",
        "src/app/cli/main.py": "from app.core import a\n",
    })

    findings = [
        finding for finding in check_architecture(project, make_design())
        if finding.kind == "may_use"
    ]

    assert len(findings) == 1
    finding = findings[0]
    assert finding.grade == "stated"
    assert finding.source == "C"
    assert finding.title == "core imports cli, which it may not use"
    assert finding.breaks == "may_use.core = []"
    assert finding.subject == "core"
    assert [(item.file, item.line, item.quote) for item in finding.evidence] == [
        ("src/app/core/a.py", 1, "from app.cli import console"),
        ("src/app/core/b.py", 5, "    from app.cli.console import x"),
    ]


def test_one_importing_module_is_the_subject(tmp_path: Path) -> None:
    """Proves the subject is the module when only one module imports across the line."""
    project = make_project(tmp_path, {
        **package("core", "cli"),
        "src/app/core/a.py": "from app.cli import b\n",
        "src/app/cli/b.py": "",
    })

    findings = check_architecture(project, make_design())

    assert [finding.subject for finding in findings if finding.kind == "may_use"] == [
        "app.core.a"
    ]


def test_an_editable_file_wins_over_a_copy_of_the_same_module(tmp_path: Path) -> None:
    """Proves a build copy of a part's package does not hide the editable file of that name."""
    project = make_project(tmp_path, {
        **package("core", "cli"),
        "src/app/core/a.py": "from app.cli import b\n",
        "src/app/cli/b.py": "",
        "build/lib/app/__init__.py": "",
        "build/lib/app/core/__init__.py": "",
        "build/lib/app/core/a.py": "",
    })

    findings = [
        finding for finding in check_architecture(project, make_design())
        if finding.kind == "may_use"
    ]

    assert len(findings) == 1
    assert [item.file for item in findings[0].evidence] == ["src/app/core/a.py"]


def test_a_listed_exception_removes_the_import_from_the_findings(tmp_path: Path) -> None:
    """Proves an exception that names a module and a part leaves only the other import."""
    project = make_project(tmp_path, {
        **package("core", "cli"),
        "src/app/cli/b.py": "",
        "src/app/core/a.py": "from app.cli import b\n",
        "src/app/core/c.py": "from app.cli import b\n",
    })
    design = make_design(
        '[[exceptions]]\nfrom = "app.core.a"\nto = "cli"\nwhy = "deliberate"\n'
    )

    findings = [
        finding for finding in check_architecture(project, design) if finding.kind == "may_use"
    ]

    assert [item.file for finding in findings for item in finding.evidence] == [
        "src/app/core/c.py"
    ]


def test_a_module_with_no_part_is_a_finding_unless_skipped(tmp_path: Path) -> None:
    """Proves a file that no prefix covers is reported and that skipped code is not."""
    project = make_project(tmp_path, {
        **package("core"),
        "src/app/core/a.py": "",
        "src/app/stray.py": "\n# hello\nVALUE = 1\n",
        "src/vendored/__init__.py": "",
    })

    findings = check_architecture(project, make_design('[skip]\nmodules = ["vendored"]\n'))

    stray = [finding for finding in findings if finding.kind == "no_part"]
    assert {finding.subject for finding in stray} == {"app", "app.stray"}
    finding = next(item for item in stray if item.subject == "app.stray")
    assert finding.grade == "stated"
    assert [(item.file, item.line, item.quote) for item in finding.evidence] == [
        ("src/app/stray.py", 2, "# hello")
    ]


def test_without_parts_no_file_lacks_a_part(tmp_path: Path) -> None:
    """Proves a design with no parts never reports files for lacking a part."""
    project = make_project(tmp_path, {**package(), "src/app/a.py": ""})
    design = read_target_design("```toml\n[skip]\nmodules = []\n```\n")

    assert check_architecture(project, design) == []


def test_cycles_are_found_at_every_level_and_cite_each_edge(tmp_path: Path) -> None:
    """Proves a cycle through a function import is found and a plain chain is not."""
    project = make_project(tmp_path, {
        **package(),
        "src/app/a.py": "from app import b\n",
        "src/app/b.py": "from app import c\n",
        "src/app/c.py": "def f():\n    from app import a\n",
        "src/app/d.py": "from app import a\n",
    })

    cycles = [
        finding for finding in check_architecture(project, None) if finding.kind == "cycle"
    ]

    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.title == "Import cycle between app.a, app.b and app.c"
    assert cycle.grade == "standard"
    assert cycle.breaks == "S2"
    assert cycle.subject == "app.a"
    assert [(item.file, item.line) for item in cycle.evidence] == [
        ("src/app/a.py", 1),
        ("src/app/b.py", 1),
        ("src/app/c.py", 2),
    ]


def test_a_self_import_is_not_a_cycle(tmp_path: Path) -> None:
    """Proves a module that imports itself or its own package does not make a cycle."""
    project = make_project(tmp_path, {**package(), "src/app/a.py": "import app.a\nimport app\n"})

    assert check_architecture(project, None) == []


def test_hidden_imports_are_one_standard_finding_per_module(tmp_path: Path) -> None:
    """Proves function imports are cited together and type checking imports are not counted."""
    project = make_project(tmp_path, {
        **package(),
        "src/app/a.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import json\n"
            "\n"
            "\n"
            "def f():\n"
            "    import os\n"
            "\n"
            "\n"
            "class C:\n"
            "    def g(self):\n"
            "        import sys\n"
        ),
    })

    findings = check_architecture(project, None)

    assert [finding.kind for finding in findings] == ["hidden_import"]
    finding = findings[0]
    assert finding.subject == "app.a"
    assert finding.grade == "standard"
    assert finding.breaks == "S2"
    assert [(item.line, item.quote.strip()) for item in finding.evidence] == [
        (7, "import os"),
        (12, "import sys"),
    ]


def kinds_of(findings: list, kind: str) -> list:
    """Returns the findings of one kind."""
    return [finding for finding in findings if finding.kind == kind]


def test_outside_world_touches_in_a_part_not_allowed_them_are_stated(tmp_path: Path) -> None:
    """Proves imports and calls of the built-in list are cited, and an allowed part is left alone."""
    project = make_project(tmp_path, {
        **package("core", "cli"),
        "src/app/core/io.py": (
            "import subprocess\n"
            "import os\n"
            "import datetime\n"
            "from urllib.request import urlopen\n"
            "\n"
            "\n"
            "def f(path):\n"
            "    print(path)\n"
            "    now = datetime.datetime.now()\n"
            "    return path.read_text(), now\n"
        ),
        "src/app/cli/main.py": "import subprocess\n\nprint('hi')\n",
    })
    design = make_design('[may_touch_outside_world]\nparts = ["cli"]\n')

    findings = kinds_of(check_architecture(project, design), "outside_world")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.subject == "app.core.io"
    assert finding.grade == "stated"
    assert finding.breaks == 'may_touch_outside_world.parts = ["cli"]'
    assert [(item.line, item.quote.strip()) for item in finding.evidence] == [
        (1, "import subprocess"),
        (4, "from urllib.request import urlopen"),
        (8, "print(path)"),
        (9, "now = datetime.datetime.now()"),
        (10, "return path.read_text(), now"),
    ]


def test_extra_outside_modules_and_clock_calls_count(tmp_path: Path) -> None:
    """Proves a module added under may_touch_outside_world.modules and time.time() are found."""
    project = make_project(tmp_path, {
        **package("core"),
        "src/app/core/a.py": "import time\nimport mylib.net\n\nVALUE = time.time()\n",
    })
    design = make_design('[may_touch_outside_world]\nparts = []\nmodules = ["mylib"]\n')

    findings = kinds_of(check_architecture(project, design), "outside_world")

    assert [(item.line, item.shows) for item in findings[0].evidence] == [
        (2, "app.core.a imports mylib.net"),
        (4, "app.core.a reads the clock with time.time()"),
    ]


def test_outside_world_is_not_checked_without_a_design(tmp_path: Path) -> None:
    """Proves a project with no design is never told it touches the outside world."""
    project = make_project(tmp_path, {**package(), "src/app/a.py": "import subprocess\nprint(1)\n"})

    assert kinds_of(check_architecture(project, None), "outside_world") == []


def test_global_statements_and_mutable_defaults_are_standard_s11(tmp_path: Path) -> None:
    """Proves global statements and mutable defaults are cited and immutable defaults are not."""
    project = make_project(tmp_path, {
        **package(),
        "src/app/a.py": (
            "COUNT = 0\n"
            "\n"
            "\n"
            "def bump():\n"
            "    global COUNT\n"
            "    COUNT += 1\n"
            "\n"
            "\n"
            "def f(a=[], b=None, c=(), *, d=dict()):\n"
            "    return a\n"
            "\n"
            "\n"
            "g = lambda x={}: x\n"
        ),
    })

    findings = check_architecture(project, None)

    global_finding = kinds_of(findings, "global_statement")[0]
    assert (global_finding.grade, global_finding.breaks) == ("standard", "S11")
    assert [item.line for item in global_finding.evidence] == [5]
    mutable = kinds_of(findings, "mutable_default")[0]
    assert mutable.breaks == "S11"
    assert [item.line for item in mutable.evidence] == [9, 9, 13]


def test_bare_and_silent_except_are_standard_s14(tmp_path: Path) -> None:
    """Proves bare excepts and handlers that only pass are cited, and handled errors are not."""
    project = make_project(tmp_path, {
        **package(),
        "src/app/a.py": (
            "def f():\n"
            "    try:\n"
            "        run()\n"
            "    except:\n"
            "        handle()\n"
            "    try:\n"
            "        run()\n"
            "    except ValueError:\n"
            "        pass\n"
            "    try:\n"
            "        run()\n"
            "    except KeyError:\n"
            "        raise\n"
        ),
    })

    findings = check_architecture(project, None)

    bare = kinds_of(findings, "bare_except")[0]
    silent = kinds_of(findings, "silent_except")[0]
    assert (bare.grade, bare.breaks) == ("standard", "S14")
    assert [(item.line, item.quote.strip()) for item in bare.evidence] == [(4, "except:")]
    assert silent.breaks == "S14"
    assert [(item.line, item.quote.strip()) for item in silent.evidence] == [
        (8, "except ValueError:")
    ]
