"""Sets up a copy of the shop example for trying ratchet on.

Run it from the code-ratchet repository's root, and give it a folder that does not exist yet:

    uv run python examples/shop/try_it.py C:\\Users\\you\\Desktop\\shop
    uv run python examples/shop/try_it.py ~/shop

It copies the example there, makes the copy a git repository with one commit, and runs
`ratchet init` in it, which keeps the example's rules and settings. Then it prints the commands
to try next. It calls no agent and costs nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from ratchet.init_project import InitError, init_project

# the example's folder
SHOP = Path(__file__).resolve().parent


def git(root: Path, *arguments: str) -> None:
    """Runs one git command in root, and stops with git's own message when it fails."""
    finished = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if finished.returncode:
        sys.exit(f"git {' '.join(arguments)} failed:\n{finished.stderr.strip()}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Give the folder to set the example up in, such as ~/shop.")
    target = Path(sys.argv[1]).expanduser().resolve()
    if target.exists():
        sys.exit(f"{target} already exists. Give a folder that does not exist yet.")

    # copy the example, leaving out this script and anything a run left behind
    shutil.copytree(SHOP, target, ignore=shutil.ignore_patterns(
        "try_it.py", ".venv", "__pycache__", ".pytest_cache"))

    # make the copy a git repository with one commit, which ratchet needs
    git(target, "init", "-q", "-b", "main")
    git(target, "add", "-A")
    # a machine with no git identity still gets its commit, made in the example's name
    has_identity = subprocess.run(["git", "config", "user.email"], cwd=target,
                                  capture_output=True, text=True).stdout.strip()
    identity = [] if has_identity else ["-c", "user.name=shop example",
                                        "-c", "user.email=shop@example.com"]
    git(target, *identity, "commit", "-q", "-m", "start")

    # set up .ratchet as its own repository, keeping the example's rules and settings
    try:
        init_project(target, assume_yes=False, confirm=None)
    except InitError as error:
        sys.exit(f"ratchet init failed: {error}")

    print(f"""The example is ready in {target}.

Look at .ratchet/rules.md and .ratchet/config.toml there first: they are the two files you
write for your own projects. Then run ratchet in that folder:

    cd "{target}"
    ratchet audit -p "look at how the total is worked out"
    ratchet problems
    ratchet refactor --max-jobs 1

If the ratchet command is not found, install it once with
    uv tool install git+https://github.com/nikhilchandra/code-ratchet
""")


if __name__ == "__main__":
    main()
