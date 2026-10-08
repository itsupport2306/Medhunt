"""Reviewed Nexus profession, offering and specialty taxonomy.

The bundled CSV is supplied by the Nexus team.  New taxonomy values take
precedence when a future sheet supplies them; the current sheet contains only
the existing values, so those are used as the canonical labels.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence


_DATA_PATH = Path(__file__).with_name("data") / "nexus_taxonomy.csv"


def normalize(value: object) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass(frozen=True)
class NexusTaxonomyRow:
    profession: str
    offering: str
    sub_offering: str
    specialty: str


def _chosen(row: dict[str, str], field: str) -> str:
    return " ".join(
        str(row.get(f"New {field}") or row.get(f"Old {field}") or "").split()
    )


@lru_cache(maxsize=1)
def rows() -> tuple[NexusTaxonomyRow, ...]:
    with _DATA_PATH.open(newline="", encoding="utf-8-sig") as handle:
        parsed = tuple(
            NexusTaxonomyRow(
                profession=_chosen(item, "Profession"),
                offering=_chosen(item, "Offering"),
                sub_offering=_chosen(item, "Sub Offering"),
                specialty=_chosen(item, "Specialty"),
            )
            for item in csv.DictReader(handle)
        )
    valid = tuple(item for item in parsed if item.profession and item.offering and item.specialty)
    if not valid:
        raise RuntimeError("The bundled Nexus taxonomy is empty or invalid.")
    return valid


@lru_cache(maxsize=1)
def professions() -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.profession for item in rows()))


def profession_labels_for_role(role: str) -> tuple[str, ...]:
    """Return canonical profession labels explicitly present in a role."""
    normalized_role = f" {normalize(role)} "
    matches = [
        label for label in professions()
        if normalize(label) and f" {normalize(label)} " in normalized_role
    ]
    matches.sort(key=lambda value: len(normalize(value)), reverse=True)
    return tuple(matches)


def specialty_labels(values: Sequence[str]) -> tuple[str, ...]:
    """Return the sheet's canonical spellings for exact source specialties."""
    wanted = {normalize(value) for value in values if normalize(value)}
    return tuple(dict.fromkeys(
        item.specialty for item in rows() if normalize(item.specialty) in wanted
    ))


def specialty_labels_for_role(
    role: str, profession_values: Sequence[str] = (),
) -> tuple[str, ...]:
    """Find approved specialty names explicitly present in a current role."""
    normalized_role = f" {normalize(role)} "
    if not normalized_role.strip():
        return ()
    wanted_professions = {
        normalize(value) for value in profession_values if normalize(value)
    }
    excluded = {"unknown", "other", "general", "contractor", "manager", "director"}
    matches: list[str] = []
    for item in rows():
        specialty = normalize(item.specialty)
        if not specialty or specialty in excluded:
            continue
        if wanted_professions and normalize(item.profession) not in wanted_professions:
            continue
        if f" {specialty} " in normalized_role:
            matches.append(item.specialty)
    matches.sort(key=lambda value: len(normalize(value)), reverse=True)
    return tuple(dict.fromkeys(matches))


def classify(
    profession_values: Sequence[str], specialty_values: Sequence[str],
) -> NexusTaxonomyRow | None:
    """Resolve one internally consistent Nexus taxonomy row.

    A profession match disambiguates specialties shared by several clinical
    professions.  Without it, a specialty is accepted only when the sheet maps
    that specialty to one profession.
    """
    wanted_specialties = {
        normalize(value) for value in specialty_values if normalize(value)
    }
    if not wanted_specialties:
        return None
    candidates = [
        item for item in rows() if normalize(item.specialty) in wanted_specialties
    ]
    if not candidates:
        return None
    wanted_professions = {
        normalize(value) for value in profession_values if normalize(value)
    }
    matched = [
        item for item in candidates if normalize(item.profession) in wanted_professions
    ]
    if matched:
        candidates = matched
    elif len({normalize(item.profession) for item in candidates}) != 1:
        return None
    # Some Allied rows intentionally repeat a specialty under a concrete sub
    # offering and "Others". Prefer the concrete classification.
    candidates.sort(key=lambda item: (
        normalize(item.sub_offering) in {"", "others"},
        normalize(item.offering), normalize(item.sub_offering),
    ))
    return candidates[0]


__all__ = [
    "NexusTaxonomyRow", "classify", "profession_labels_for_role",
    "professions", "rows", "specialty_labels", "specialty_labels_for_role",
]
