from app.database import transaction, utc_now

TARGETS = {"Brenda", "Mariela"}

with transaction(immediate=True) as conn:
    rows = conn.execute(
        """SELECT name FROM comment_professionals
           WHERE feedback_kind='POSITIVE'
             AND lower(name) IN ('brenda', 'mariela')"""
    ).fetchall()
    found = {row["name"] for row in rows}
    if found != TARGETS:
        raise SystemExit(f"Retomada cancelada: profissionais encontradas={sorted(found)}")
    now = utc_now()
    conn.execute(
        """UPDATE comment_professionals SET enabled=1, updated_at=?
           WHERE feedback_kind='POSITIVE'
             AND lower(name) IN ('brenda', 'mariela')""",
        (now,),
    )
print("Retomada automática ativada para Brenda e Mariela.")
