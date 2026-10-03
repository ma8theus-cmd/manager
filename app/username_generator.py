from __future__ import annotations

import random
import re
import unicodedata

USERNAME_RE = re.compile(r"^[A-Za-z0-9]+(?:[._][A-Za-z0-9]+)?$")


def ascii_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9]", "", normalized)


def is_valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def username_candidates(first_name: str, last_name: str, seed: int | None = None):
    """Yield deterministic but varied, human-style username candidates.

    The site accepts letters/digits and at most one internal '.' or '_'.
    We deliberately mix common personal patterns instead of forcing every
    account into ``First_LastNN``.  A seed keeps retries reproducible.
    """
    first = ascii_token(first_name)
    last = ascii_token(last_name)
    if not first or not last:
        raise ValueError("Nome/sobrenome não geraram tokens válidos.")

    rng = random.Random(seed)
    f = first.lower()
    l = last.lower()
    fi = f[:1]
    li = l[:1]

    # Common styles people naturally choose. No numeric suffix is preferred.
    plain = [
        f"{f}{l}", f"{f}.{l}", f"{f}_{l}",
        f"{fi}{l}", f"{fi}.{l}", f"{fi}_{l}",
        f"{f}{li}", f"{f}.{li}", f"{f}_{li}",
        f"{l}{f}", f"{l}.{f}", f"{l}_{f}",
        f"{l}{fi}", f"{l}.{fi}",
    ]
    rng.shuffle(plain)

    seen: set[str] = set()
    for candidate in plain:
        if candidate not in seen and is_valid_username(candidate):
            seen.add(candidate)
            yield candidate

    # If a simple form is taken, vary with short, non-sequential-looking suffixes.
    nums = list(range(1, 100))
    rng.shuffle(nums)
    numbered_bases = [f, f"{f}{li}", f"{fi}{l}", f"{f}.{l}", f"{f}_{l}", l]
    rng.shuffle(numbered_bases)
    for n in nums:
        for base in numbered_bases:
            candidate = f"{base}{n}"
            if candidate not in seen and is_valid_username(candidate):
                seen.add(candidate)
                yield candidate
