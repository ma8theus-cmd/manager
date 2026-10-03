from __future__ import annotations

from app.database import add_event, transaction, utc_now


def mark_authorized_flow_started(member_id: int) -> None:
    """
    Record that this member is being processed through the previously
    authorized automated +18 / Terms flow.

    This is an operational audit event; it does not manufacture or replace the
    underlying authorization that was obtained outside the automation.
    """
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT id FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        add_event(
            conn,
            member_id,
            "AUTHORIZED_REGISTRATION_AUTOMATION",
            "Fluxo automático de maioridade e Terms iniciado para membro previamente autorizado.",
        )


def mark_account_created_automatic(member_id: int) -> str:
    """Persist a successfully submitted/accepted registration."""
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT registration_status FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")

        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='WAITING_EMAIL_CONFIRMATION',
                email_status='PENDING',
                form_prepared_at=COALESCE(form_prepared_at, ?),
                account_created_at=?,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, now, now, member_id),
        )
        add_event(
            conn,
            member_id,
            "ACCOUNT_CREATED_AUTOMATIC",
            "Conta criada automaticamente após confirmação +18, aceite dos Terms e envio do Register.",
        )
        return now
