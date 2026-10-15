"""Keep batch ATS delivery concurrent without sharing Nexus HTTP clients."""
import threading
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import ceipal_delivery, nexus_delivery, nexus_sync


@pytest.mark.parametrize("destination", ["nexus", "ceipal"])
def test_ats_delivery_starts_two_independent_workers(monkeypatch, destination):
    module = nexus_delivery if destination == "nexus" else ceipal_delivery
    ready = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def process_once():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                ready.set()
        release.wait(5)
        with lock:
            active -= 1
        return None

    monkeypatch.setattr(module, "process_once", process_once)
    if destination == "nexus":
        monkeypatch.setattr(module, "worker_enabled", lambda: True)
        monkeypatch.setattr(module.config, "NEXUS_DELIVERY_WORKERS", 2)
        monkeypatch.setattr(module.nexus_sync, "close_thread_client", lambda: None)
    else:
        monkeypatch.setattr(module.config, "CEIPAL_DELIVERY_WORKERS", 2)
        monkeypatch.setattr(module.config, "MEDHUNT_HEALTHBOARD_SERVICE_TOKEN", "test")
        monkeypatch.setattr(module.healthboard_auth, "enabled", lambda: True)
    try:
        module.start()
        assert ready.wait(5), "both ATS workers should begin work"
        assert peak == 2
    finally:
        release.set()
        module.stop(timeout=5)
    assert not module._THREADS


def test_nexus_clients_are_reused_only_within_their_worker(monkeypatch):
    created = []
    closed = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    class FakeClient:
        def __init__(self, settings):
            self.settings = settings
            with lock:
                created.append(self)

        def close(self):
            with lock:
                closed.append(self)

    monkeypatch.setattr(nexus_sync, "NexusClient", FakeClient)
    settings = nexus_sync.NexusSettings(enabled=True, base_url="https://nexus.example")
    results = []

    def worker():
        first = nexus_sync._shared_client(settings)
        barrier.wait(timeout=5)
        second = nexus_sync._shared_client(settings)
        with lock:
            results.append((first, second))
        nexus_sync.close_thread_client()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(created) == 2
    assert all(first is second for first, second in results)
    assert results[0][0] is not results[1][0]
    assert len(closed) == 2
