from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.username_generator import is_valid_username, username_candidates


def main():
    a = list(username_candidates("Rémy", "Orteaux", seed=4))[:40]
    b = list(username_candidates("Rémy", "Orteaux", seed=4))[:40]
    assert a == b, "geração precisa ser determinística por membro"
    assert len(a) == len(set(a)) >= 20
    assert all(is_valid_username(x) for x in a)
    assert any("_" not in x and "." not in x for x in a[:10])
    assert any(not x[-1].isdigit() for x in a[:10])
    robotic = [x for x in a[:10] if "_" in x and x[-1].isdigit()]
    assert len(robotic) < 5
    print("OK - usernames variados, válidos e determinísticos.")

if __name__ == "__main__":
    main()
