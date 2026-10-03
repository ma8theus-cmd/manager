\
from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.database import init_db, transaction, utc_now


def is_adult(birth_date: date) -> bool:
    today = date.today()
    years = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
    return years >= 18


def main():
    init_db()

    csv_path = settings.csv_path
    if not csv_path.exists():
        raise SystemExit(f"CSV não encontrado: {csv_path}")

    inserted = 0
    updated = 0
    invalid = []

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        required = {"id", "first_name", "last_name", "birth_date", "city_france"}
        if not required.issubset(reader.fieldnames or []):
            raise SystemExit(
                f"CSV precisa das colunas {sorted(required)}. Recebido: {reader.fieldnames}"
            )

        with transaction(immediate=True) as conn:
            for line_no, row in enumerate(reader, start=2):
                try:
                    member_id = int(row["id"])
                    first = row["first_name"].strip()
                    last = row["last_name"].strip()
                    city = row["city_france"].strip()
                    dob = datetime.strptime(row["birth_date"].strip(), "%Y-%m-%d").date()

                    if not first or not last or not city:
                        raise ValueError("nome/sobrenome/cidade vazio")
                    if not is_adult(dob):
                        raise ValueError("membro menor de 18 anos")

                    exists = conn.execute(
                        "SELECT id FROM members WHERE id=?",
                        (member_id,),
                    ).fetchone()

                    now = utc_now()
                    if exists:
                        conn.execute(
                            """
                            UPDATE members
                            SET first_name=?, last_name=?, birth_date=?, city_france=?, updated_at=?
                            WHERE id=?
                            """,
                            (first, last, dob.isoformat(), city, now, member_id),
                        )
                        updated += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO members(
                                id, first_name, last_name, birth_date, city_france,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (member_id, first, last, dob.isoformat(), city, now, now),
                        )
                        inserted += 1

                except Exception as exc:
                    invalid.append((line_no, str(exc)))

    print(f"Importação concluída.")
    print(f"Novos: {inserted}")
    print(f"Atualizados: {updated}")
    print(f"Inválidos: {len(invalid)}")
    for line_no, error in invalid[:30]:
        print(f"  linha {line_no}: {error}")


if __name__ == "__main__":
    main()
