from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import argparse

from app.database import init_db
from app.scout import record_comment_published


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Registra no Scout um comentário que já foi publicado com sucesso."
    )
    parser.add_argument("--member-id", type=int, required=True)
    parser.add_argument("--ad-title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--comment", required=True)
    parser.add_argument("--published-at")
    args = parser.parse_args()

    init_db()
    scout_id = record_comment_published(
        args.member_id,
        args.ad_title,
        args.url,
        args.comment,
        published_at=args.published_at,
    )
    print(f"Scout #{scout_id} criado/recuperado com sucesso.")


if __name__ == "__main__":
    main()
