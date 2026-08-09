"""Tests import the slot modules the same way server.py does."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def free_ports(monkeypatch):
    """Pretend every port is free.

    can_start() probes the real machine, so a test using port 8771 passed or
    failed depending on whether an instance happened to be running outside the
    test run. A suite that answers differently on the same code is worse than
    no suite. The probe itself is covered by its own test.
    """
    import instances
    monkeypatch.setattr(instances, "_port_busy", lambda port: False)


@pytest.fixture(autouse=True)
def no_metric_polling(monkeypatch):
    """Keep the background metrics thread out of the tests.

    It reaches over the network; nothing here should.
    """
    import instances
    monkeypatch.setattr(instances.InstanceManager, "_poll_loop", lambda self: None)
