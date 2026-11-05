"""Checks the facts read from a project's code, on small packages."""

from __future__ import annotations

from pathlib import Path

from ratchet.architecture import Module
from ratchet.audit import code_facts
from ratchet.audit.code_facts import NO_AREA, InventoryItem, area_of, build_inventory, enclosing_name
from test_architecture import make_design, make_project, package

MODEL = (
    "class Order:\n"
    "    class Line:\n"
    "        pass\n"
    "\n"
    "    def total(self):\n"
    "        def inner():\n"
    "            return 1\n"
    "        return inner()\n"
    "\n"
    "\n"
    "@decorate\n"
    "class Plain:\n"
    "    pass\n"
    "\n"
    "\n"
    "X = 1\n"
)


def test_inventory_lists_modules_and_classes(tmp_path: Path) -> None:
    """Proves modules, classes, nested classes and decorators are listed with lines and sizes."""
    project = make_project(tmp_path, {**package("core"), "src/app/core/model.py": MODEL})

    items = build_inventory(project, None)

    model = [item for item in items if item.name.startswith("app.core.model")]
    assert model == [
        InventoryItem("app.core.model", "module", "src/app/core/model.py", 1, 16),
        InventoryItem("app.core.model.Order", "class", "src/app/core/model.py", 1, 8),
        InventoryItem("app.core.model.Order.Line", "class", "src/app/core/model.py", 2, 2),
        InventoryItem("app.core.model.Plain", "class", "src/app/core/model.py", 11, 3),
    ]


def test_a_file_that_does_not_parse_is_listed_as_an_empty_module(tmp_path: Path) -> None:
    """Proves a broken file still gets a module item."""
    project = make_project(tmp_path, {**package(), "src/app/broken.py": "def (:\n"})

    items = build_inventory(project, None)

    assert InventoryItem("app.broken", "module", "src/app/broken.py", 1, 0) in items


def test_enclosing_name_finds_the_innermost_definition(tmp_path: Path) -> None:
    """Proves a line maps to its method, nested function, class or else the module."""
    project = make_project(tmp_path, {**package("core"), "src/app/core/model.py": MODEL})
    module = Module("app.core.model", "src/app/core/model.py", False)

    assert enclosing_name(project, module, 7) == "app.core.model.Order.total.inner"
    assert enclosing_name(project, module, 5) == "app.core.model.Order.total"
    assert enclosing_name(project, module, 3) == "app.core.model.Order.Line"
    assert enclosing_name(project, module, 11) == "app.core.model.Plain"
    assert enclosing_name(project, module, 16) == "app.core.model"


def test_enclosing_name_of_a_broken_file_is_the_module(tmp_path: Path) -> None:
    """Proves an unreadable file gives the module's own name."""
    project = make_project(tmp_path, {**package(), "src/app/broken.py": "def (:\n"})

    assert enclosing_name(project, Module("app.broken", "src/app/broken.py", False), 1) == "app.broken"


def test_tests_reaching_finds_tests_that_import_the_modules(tmp_path: Path) -> None:
    """Proves tests are matched by their imports, and tests of other modules are not."""
    project = make_project(tmp_path, {
        **package("core"),
        "src/app/core/model.py": "",
        "src/app/core/other.py": "",
        "tests/test_model.py": "from app.core.model import Order\n",
        "tests/test_package.py": "import app.core.model\n",
        "tests/test_other.py": "import app.core.other\n",
    })

    found = code_facts.tests_reaching(project, {"app.core.model"})

    assert found == ["tests/test_model.py", "tests/test_package.py"]


def test_area_is_the_part_of_the_subjects_module() -> None:
    """Proves a class or function takes its module's part, and a stray module has no area."""
    design = make_design()

    assert area_of(design, "app.core.model.Order.total") == "core"
    assert area_of(design, "app.cli") == "cli"
    assert area_of(design, "elsewhere.thing") == NO_AREA
    assert area_of(None, "app.core") == NO_AREA
