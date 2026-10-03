from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from research.comment_research_collector import (
    _contains_any,
    _normalize_filter_text,
    is_acceptable_comment,
)
from app.comment_automation.feedback import normalize_feedback_kind


_HEADER_RE = re.compile(
    r"^([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ '\-]*)\s+[—-]\s+\d+\s+COMMENTAIRES?\s*$"
)
_ITEM_RE = re.compile(r"^(\d+)\.\s*(.+)$")
UNASSIGNED_NEGATIVE_PROFESSIONAL = "Feedbacks negativos sem anúncio"

# The existing research filter is intentionally broad. Imports into the
# operational panel use this additional gate for sexual, appearance-only, or
# non-massage wording; original source text is never rewritten.
_IMPORT_REJECT_TERMS = (
    "escort*", "escorte*", "prelim*", "sexuel*", "erot*",
    "sensuel*", "sensual*", "coquin*", "excitant*", "desir*",
    "baiser*", "embrass*", "caress*", "corps", "courbe*",
    "seins*", "poitrine", "fess*", "cul", "sexy", "hot",
    "chaud*", "jouiss*", "orgasm*", "penetr*", "position*",
    "lit", "nue*", "deshab*", "intim*", "social time",
    "champagne", "douche", "gorge profonde", "encaisse*",
)

def _canonical_name(value: str) -> str:
    return " ".join(value.split()).title()


def parse_comment_txt(
    body: str,
    *,
    default_professional: str | None = None,
) -> list[dict[str, object]]:
    """Parse sections and numbered comments without rewriting their wording."""
    rows: list[dict[str, object]] = []
    professional: str | None = default_professional
    item_no: int | None = None
    item_lines: list[str] = []

    def flush() -> None:
        nonlocal item_no, item_lines
        if professional is not None and item_no is not None:
            text = " ".join(part.strip() for part in item_lines if part.strip()).strip()
            if text:
                rows.append({
                    "professional": professional,
                    "item_no": item_no,
                    "text": text,
                })
        item_no = None
        item_lines = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        header = _HEADER_RE.match(line)
        if header:
            flush()
            professional = _canonical_name(header.group(1))
            continue

        item = _ITEM_RE.match(line)
        if item:
            flush()
            item_no = int(item.group(1))
            item_lines = [item.group(2).strip()]
            continue

        if professional is not None and item_no is not None and line and not set(line) <= {"="}:
            item_lines.append(line)

    flush()
    return rows


def is_comment_eligible(text: str) -> bool:
    if not is_acceptable_comment(text):
        return False
    normalized = re.sub(r"\s+", " ", _normalize_filter_text(text)).strip()
    return not _contains_any(normalized, _IMPORT_REJECT_TERMS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _professional_id(
    conn: sqlite3.Connection,
    name: str,
    now: str,
    feedback_kind: str = "POSITIVE",
) -> int:
    kind = normalize_feedback_kind(feedback_kind)
    columns = _table_columns(conn, "comment_professionals")

    if "feedback_kind" in columns:
        row = conn.execute(
            "SELECT id,feedback_kind FROM comment_professionals WHERE lower(name)=lower(?)",
            (name,),
        ).fetchone()
        if row:
            if normalize_feedback_kind(row[1]) != kind:
                raise ValueError(
                    f"Profissional {name} já pertence à aba de feedbacks oposta."
                )
            return int(row[0])
        cursor = conn.execute(
            """INSERT INTO comment_professionals
               (name,target_url,enabled,feedback_kind,created_at,updated_at)
               VALUES (?,?,1,?,?,?)""",
            (name, None, kind, now, now),
        )
        return int(cursor.lastrowid)

    if kind != "POSITIVE":
        raise ValueError("Banco antigo sem separação de feedbacks; inicialize o banco primeiro.")

    row = conn.execute(
        "SELECT id FROM comment_professionals WHERE lower(name)=lower(?)",
        (name,),
    ).fetchone()
    if row:
        return int(row[0])
    cursor = conn.execute(
        """INSERT INTO comment_professionals
           (name,target_url,enabled,created_at,updated_at)
           VALUES (?,?,1,?,?)""",
        (name, None, now, now),
    )
    return int(cursor.lastrowid)


def insert_records(
    conn: sqlite3.Connection,
    records: Iterable[Mapping[str, object]],
    *,
    feedback_kind: str = "POSITIVE",
    now: str | None = None,
) -> dict[str, object]:
    """Insert eligible records atomically within the caller's transaction."""
    records = list(records)
    kind = normalize_feedback_kind(feedback_kind)
    timestamp = now or _now()
    bank_columns = _table_columns(conn, "comment_bank")
    next_source = int(
        conn.execute(
            "SELECT COALESCE(MAX(source_no), 0) + 1 FROM comment_bank"
        ).fetchone()[0]
    )
    summary: dict[str, object] = {
        "received": len(records),
        "eligible": 0,
        "rejected": 0,
        "duplicates": 0,
        "inserted": 0,
        "by_professional": {},
        "feedback_kind": kind,
    }
    seen: set[str] = set()

    for record in records:
        name = _canonical_name(str(record.get("professional") or ""))
        if kind == "NEGATIVE" and not name:
            name = UNASSIGNED_NEGATIVE_PROFESSIONAL
        text = str(record.get("text") or "").strip()
        if not name or not text or not is_comment_eligible(text):
            summary["rejected"] = int(summary["rejected"]) + 1
            continue

        summary["eligible"] = int(summary["eligible"]) + 1
        key = text
        if key in seen:
            summary["duplicates"] = int(summary["duplicates"]) + 1
            continue

        professional_id = _professional_id(conn, name, timestamp, kind)
        if "feedback_kind" in bank_columns:
            existing = conn.execute(
                """SELECT 1 FROM comment_bank
                   WHERE comment_text=? AND feedback_kind=? LIMIT 1""",
                (text, kind),
            ).fetchone()
        else:
            if kind != "POSITIVE":
                raise ValueError("Banco antigo sem separação de feedbacks; inicialize o banco primeiro.")
            existing = conn.execute(
                """SELECT 1 FROM comment_bank
                   WHERE comment_text=? LIMIT 1""",
                (text,),
            ).fetchone()
        if existing:
            summary["duplicates"] = int(summary["duplicates"]) + 1
            seen.add(key)
            continue

        item_no = record.get("item_no", "")
        age_band = f"TXT importado · item {item_no}"
        if "feedback_kind" in bank_columns:
            conn.execute(
                """INSERT INTO comment_bank
                   (source_no,professional_id,age_band,comment_text,feedback_kind,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (next_source, professional_id, age_band, text, kind, "AVAILABLE", timestamp, timestamp),
            )
        else:
            conn.execute(
                """INSERT INTO comment_bank
                   (source_no,professional_id,age_band,comment_text,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (next_source, professional_id, age_band, text, "AVAILABLE", timestamp, timestamp),
            )
        next_source += 1
        summary["inserted"] = int(summary["inserted"]) + 1
        by_professional = summary["by_professional"]
        assert isinstance(by_professional, dict)
        by_professional[name] = int(by_professional.get(name, 0)) + 1
        seen.add(key)

    return summary


def read_comment_file(
    path: str | Path,
    *,
    default_professional: str | None = None,
) -> list[dict[str, object]]:
    source = Path(path)
    return parse_comment_txt(
        source.read_text(encoding="utf-8-sig"),
        default_professional=default_professional,
    )
