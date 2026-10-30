"""Checks that ratchet is installed and can see the parts of agent-rooster it is built on."""

from __future__ import annotations


def test_ratchet_imports() -> None:
    """Proves the ratchet package is installed and importable."""
    import ratchet

    assert ratchet.__doc__


def test_agent_rooster_has_the_api_ratchet_needs() -> None:
    """Proves ratchet sees agent-rooster's current API, which exists only on its 'new' branch,
    so a checkout of the old 'main' fails here rather than in the middle of a job."""
    import rooster

    for name in ("Agent", "AgentGraph", "Command", "Job", "JobStatus", "Model", "Tool"):
        assert hasattr(rooster, name), f"agent-rooster has no {name}. Is it on the 'new' branch?"
