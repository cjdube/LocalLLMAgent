"""Guards the split between requirements.txt (intent) and requirements.lock
(what actually gets installed).

Two files describing the same dependencies can disagree, and the disagreement is
silent in the direction that matters: bump a pin in requirements.txt, forget to
regenerate the lock, and every install keeps using the old version while the
file you edited says otherwise. Nothing at runtime reads requirements.txt, so
nothing would ever contradict you.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)")


def _pins(filename: str) -> dict[str, str]:
    pins = {}
    for line in (ROOT / filename).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN.match(line)
        if m:
            # Normalized the way pip compares names: case-insensitive, and
            # "-"/"_"/"." interchangeable (google_genai == google-genai).
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def test_lock_is_not_empty():
    assert len(_pins("requirements.lock")) > len(_pins("requirements.txt"))


def test_every_direct_pin_appears_in_the_lock_at_the_same_version():
    direct = _pins("requirements.txt")
    lock = _pins("requirements.lock")
    assert direct, "requirements.txt has no pins — parser broken?"

    missing = sorted(name for name in direct if name not in lock)
    assert not missing, (
        "in requirements.txt but absent from requirements.lock "
        f"(regenerate the lock): {missing}"
    )

    mismatched = {
        name: (want, lock[name])
        for name, want in direct.items()
        if lock[name] != want
    }
    assert not mismatched, (
        "requirements.txt and requirements.lock disagree "
        f"(txt, lock): {mismatched}"
    )
