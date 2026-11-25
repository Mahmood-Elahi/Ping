"""Exact source profiles for counted HTML collectors."""

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Callable, NamedTuple

from . import counted_radancy, etsy
from ..models import AdapterError


class CountedProfile(NamedTuple):
    target_sha256: str
    collect: Callable
    max_pages: int
    max_jobs: int


PROFILES = {
    "Applied Materials": CountedProfile(
        "6fe0790890aa73a749ba6fa960d96c728a206576b22b9191a8cc3ec28ff32b2d",
        counted_radancy.fetch_jobs,
        160,
        2400,
    ),
    "BlackRock": CountedProfile(
        "36eb5942218e0d90ec879a2b9ab4eee8dd5b556c3272cbf56c1e1ce69caf3604",
        counted_radancy.fetch_jobs,
        50,
        1000,
    ),
    "Chevron": CountedProfile(
        "4949acf2065e7fd85a015cfbea4cf47acb215a0a3e0d01ae8da9f2b960e9f131",
        counted_radancy.fetch_jobs,
        50,
        1000,
    ),
    "Etsy": CountedProfile(
        "c4d3dc74e574081c94e798ba987cf9e09eb9f1410ee6dbcbdc23639d1ed91971",
        etsy.fetch_jobs,
        12,
        360,
    ),
}


def counted_profile(target):
    profile = PROFILES.get(target.name)
    if target.pagination_contract != "radancy_counted_pages" and not (
        target.name == "Etsy" and target.max_pages > 2
    ):
        return None
    raw = json.dumps(asdict(target), sort_keys=True, separators=(",", ":")).encode()
    if profile is None or sha256(raw).hexdigest() != profile.target_sha256:
        raise AdapterError("Counted HTML listing differs from its reviewed target")
    return profile
