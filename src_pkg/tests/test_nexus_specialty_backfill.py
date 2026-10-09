from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_nexus_specialties.py"
spec = importlib.util.spec_from_file_location("backfill_nexus_specialties", SCRIPT)
assert spec and spec.loader
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


PROFESSIONS = [
    {"professionId": 10, "name": "RN", "active": True},
    {"professionId": 99, "name": "Unknown", "active": True},
]
SPECIALTIES = [
    {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
    {"specialtyId": 21, "professionId": 10, "name": "ICU", "active": True},
    {"specialtyId": 22, "professionId": 10, "name": "Med Surg", "active": True},
    {"specialtyId": 199, "professionId": 99, "name": "Unknown", "active": True},
]


def test_backfill_uses_resume_headline_and_preserves_rn_profession():
    candidate = {"professionIds": [10], "specialtyIds": [20], "primarySpecialtyId": 20}
    extraction = {
        "status": "extracted", "conflicts": [],
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
    }
    assert backfill.needs_update(candidate, {20, 199})
    assert backfill.classification(candidate, extraction, PROFESSIONS, SPECIALTIES) == (10, (21, 22), 21)


def test_backfill_uses_first_resume_specialty_when_no_headline_specialty():
    candidate = {"professionIds": [10], "specialtyIds": [], "primarySpecialtyId": None}
    extraction = {
        "status": "extracted", "conflicts": [],
        "fields": {"job_title": "Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
    }
    assert backfill.classification(candidate, extraction, PROFESSIONS, SPECIALTIES) == (10, (21, 22), 21)


def test_backfill_can_replace_unknown_profession_from_clear_role():
    candidate = {"professionIds": [99], "specialtyIds": [199], "primarySpecialtyId": 199}
    extraction = {
        "status": "extracted", "conflicts": [],
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
    }
    assert backfill.classification(candidate, extraction, PROFESSIONS, SPECIALTIES) == (10, (21,), 21)


def test_backfill_fills_both_missing_profession_and_specialties():
    candidate = {"professionIds": [], "specialtyIds": [], "primarySpecialtyId": None}
    extraction = {
        "status": "extracted", "conflicts": [],
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
    }
    assert backfill.needs_update(candidate, {20, 199})
    assert backfill.classification(candidate, extraction, PROFESSIONS, SPECIALTIES) == (10, (21, 22), 21)


def test_backfill_uses_single_nursing_license_when_role_missing():
    candidate = {"professionIds": [], "specialtyIds": [], "primarySpecialtyId": None}
    extraction = {
        "status": "extracted", "conflicts": [],
        "fields": {"licenses": ["Registered Nurse (RN)"], "specialties": ["Med Surg", "ICU"]},
        "confidence": {"specialties": 0.76},
    }
    assert backfill.classification(candidate, extraction, PROFESSIONS, SPECIALTIES) == (10, (22, 21), 22)
