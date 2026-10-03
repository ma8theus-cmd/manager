#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.comment_automation.importer import (
    UNASSIGNED_NEGATIVE_PROFESSIONAL,
    insert_records,
    read_comment_file,
)
from app.database import transaction


def main() -> int:
    parser = argparse.ArgumentParser(description="Importa feedbacks TXT no banco do Manager.")
    parser.add_argument("source", type=Path, help="Arquivo TXT com seções por profissional.")
    parser.add_argument(
        "--feedback-kind",
        choices=("POSITIVE", "NEGATIVE"),
        default="POSITIVE",
        help="Fila do painel que receberá os feedbacks.",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"Arquivo não encontrado: {args.source}")

    default_professional = (
        UNASSIGNED_NEGATIVE_PROFESSIONAL
        if args.feedback_kind == "NEGATIVE"
        else None
    )
    records = read_comment_file(
        args.source,
        default_professional=default_professional,
    )
    if not records:
        parser.error("Nenhum comentário numerado foi encontrado no TXT.")

    with transaction(immediate=True) as conn:
        summary = insert_records(
            conn,
            records,
            feedback_kind=args.feedback_kind,
        )

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
