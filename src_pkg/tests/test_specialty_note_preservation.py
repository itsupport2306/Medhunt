"""Captured specialty evidence must survive a shorter profile refresh."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import config, store, nexus_sync


def test_shorter_profile_refresh_keeps_new_specialty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "specialty.db"))
    first = store.upsert_candidate_profiles([{
        "name": "Jane Example", "location": "Austin, TX", "source": "usnews",
        "source_id": "provider-123", "notes": "Role: Registered Nurse\n" + "Old card detail. " * 30,
    }])[0]
    refreshed = store.upsert_candidate_profiles([{
        "name": "Jane Example", "location": "Austin, TX", "source": "usnews",
        "source_id": "provider-123", "notes": "Role: Registered Nurse\nSpecialty: Operating Room",
    }])[0]
    assert refreshed["id"] == first["id"]
    saved = store.get_candidate(first["id"])
    assert "Specialty: Operating Room" in saved["notes"]
    assert "Old card detail" in saved["notes"]
    assert nexus_sync._candidate_specialties(saved, {}) == ["Operating Room"]

    store.upsert_candidate_profiles([{
        "name": "Jane Example", "location": "Austin, TX", "source": "usnews",
        "source_id": "provider-123", "notes": "Specialty: Critical Care",
    }])
    latest = store.get_candidate(first["id"])
    assert nexus_sync._candidate_specialties(latest, {})[:2] == ["Critical Care", "Operating Room"]
