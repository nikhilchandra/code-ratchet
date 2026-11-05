"""Checks the reading of the target design from rules.md."""

from __future__ import annotations

import pytest

from ratchet.init_project import TEMPLATES
from ratchet.target_design import (
    TEMPLATE_MARKER,
    TargetDesignError,
    is_template,
    part_of,
    read_target_design,
)


def block(body: str) -> str:
    """Returns rules text with the given TOML body inside a fenced block."""
    return f"# Rules\n\nSome prose.\n\n```toml\n{body}\n```\n"


def test_the_marker_is_found_only_on_the_first_line() -> None:
    """Proves the template marker counts on line one and nowhere else."""
    assert is_template(TEMPLATE_MARKER + "\n# Rules\n")
    assert is_template("﻿" + TEMPLATE_MARKER + "\r\n# Rules\n")
    assert not is_template("# Rules\n" + TEMPLATE_MARKER + "\n")
    assert not is_template("")


def test_the_shipped_template_has_the_marker_and_a_readable_block() -> None:
    """Proves the template's example names parse into a design."""
    text = (TEMPLATES / "rules.md").read_text(encoding="utf-8")

    design = read_target_design(text)

    assert is_template(text)
    assert design is not None
    assert design.parts == {"core": ("example.core",), "cli": ("example.cli",)}
    assert design.may_use == {"core": (), "cli": ("core",)}
    assert design.outside_parts == ("cli",)
    assert part_of(design, "example.cli.main") == "cli"


def test_prose_with_no_block_gives_no_design() -> None:
    """Proves rules written only in prose are not an error."""
    assert read_target_design("# Rules\n\nThe core may not use the cli.\n") is None


def test_a_full_block_is_read() -> None:
    """Proves lists of prefixes, exceptions, extra modules and the skip list are read."""
    design = read_target_design(block("""
[parts]
core = ["app.core", "app.model"]
cli = "app.cli"

[may_use]
core = []
cli = ["core"]

[may_touch_outside_world]
parts = ["cli"]
modules = ["boto3"]

[[exceptions]]
from = "app.core.audit"
to = "app.cli.console"
why = "audit prints its own report"

[skip]
modules = ["app.generated"]
"""))

    assert design is not None
    assert design.parts["core"] == ("app.core", "app.model")
    assert design.outside_modules == ("boto3",)
    assert design.exceptions[0].source == "app.core.audit"
    assert design.exceptions[0].why == "audit prints its own report"
    assert design.skip_modules == ("app.generated",)


def test_the_most_specific_prefix_decides_the_part() -> None:
    """Proves a longer prefix wins and that a prefix does not match half a word."""
    design = read_target_design(block("""
[parts]
app = "app"
views = "app.views"

[may_use]
app = []
views = ["app"]
"""))

    assert design is not None
    assert part_of(design, "app.views.strip") == "views"
    assert part_of(design, "app.models") == "app"
    assert part_of(design, "app") == "app"
    assert part_of(design, "apple") is None


@pytest.mark.parametrize("body, message", [
    ('[parts]\na = "x"\nb = "y"\n[may_use]\na = []', "does not name the parts: b"),
    ('[parts]\na = "x"\n[may_use]\na = []\nz = []', "'z', which is not a part"),
    ('[parts]\na = "x"\n[may_use]\na = ["z"]', "names 'z', which is not a part"),
    ('[parts]\na = "x"\nb = "x"\n[may_use]\na = []\nb = []', "both use the prefix"),
    ('[parts]\na = "x"\n[may_use]\na = []\n[may_touch_outside_world]\nparts = ["q"]',
     "'q', which is not a part"),
    ('[parts]\na = "x"\n[may_use]\na = []\n[[exceptions]]\nfrom = "x"', "from, to and why"),
    ("[parts\n", "not valid"),
    ("[extra]\nx = 1", "unknown sections: extra"),
])
def test_a_block_that_does_not_agree_with_itself_is_refused(body: str, message: str) -> None:
    """Proves each way the parts, may_use and the other tables can disagree is named."""
    with pytest.raises(TargetDesignError, match=message):
        read_target_design(block(body))


def test_two_blocks_are_refused() -> None:
    """Proves a second toml block is an error rather than being ignored."""
    text = block("[skip]\nmodules = []") + block("[skip]\nmodules = []")

    with pytest.raises(TargetDesignError, match="more than one"):
        read_target_design(text)
