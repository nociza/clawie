from __future__ import annotations

import random
from collections.abc import Iterable


DEFAULT_AGENT_NAMES: list[str] = [
    "Abulafia",
    "Diotallevi",
    "Casaubon",
    "Belbo",
    "Ardenti",
    "Aglie",
    "Garamond",
    "Manutius",
    "Malkuth",
    "Kether",
    "Sephirot",
    "Telluric",
    "Pendulum",
    "Synarchici",
    "Resurgentes",
    "Rosicrucian",
    "Incandenza",
    "Eschaton",
    "Annular",
    "Ennet",
    "Antitoi",
    "Marathe",
    "Pemulis",
    "Samizdat",
    "Concavity",
    "Convexity",
    "InterLace",
    "Infernatron",
    "Mimetic",
    "Mansoul",
    "Vernall",
    "Hamtun",
    "Ultraduct",
    "Porthimoth",
    "Norhan",
    "AtticsBreath",
    "ForbiddenWorlds",
    "SleeplessSwords",
    "RoodWall",
    "ChainOffice",
    "RaftersBeams",
    "CloudsUnfold",
    "BurningGold",
    "EatingFlowers",
    "Bloomsday",
    "Nighttown",
    "Sweny",
    "Eccles",
    "Metempsychosis",
    "Parallax",
    "Kinch",
    "Poldy",
    "Agenbite",
    "Inwit",
    "Sandymount",
    "Helys",
    "Dlugacz",
]


def choose_default_agent_name(existing_names: Iterable[str] = ()) -> str:
    existing = {str(name).strip().casefold() for name in existing_names}
    candidates = [name for name in DEFAULT_AGENT_NAMES if name.casefold() not in existing]
    if not candidates:
        raise ValueError("all default agent names are already in use; pass an explicit agent_id")
    return random.choice(candidates)
