FEEDBACK_POSITIVE = "POSITIVE"
FEEDBACK_NEGATIVE = "NEGATIVE"
FEEDBACK_KINDS = frozenset({FEEDBACK_POSITIVE, FEEDBACK_NEGATIVE})


def normalize_feedback_kind(value: str | None = None) -> str:
    kind = (value or FEEDBACK_POSITIVE).strip().upper()
    if kind not in FEEDBACK_KINDS:
        raise ValueError("Tipo de feedback inválido; use POSITIVE ou NEGATIVE.")
    return kind


def filter_feedback_rows(rows, feedback_kind: str):
    kind = normalize_feedback_kind(feedback_kind)
    return [
        row for row in rows
        if normalize_feedback_kind(row.get("feedback_kind")) == kind
    ]
