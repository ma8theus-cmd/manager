"""Read-only view of saved promising-member comments."""
import hashlib
import sqlite3
from .config import settings
from research.comment_research_collector import is_acceptable_comment, score_comment


def promising_members_data():
    path = settings.project_root / "research" / "comment_research.db"
    if not path.exists():
        return {"members": [], "error": None}
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            logins = {}
            has_alerts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_alerts'"
            ).fetchone()
            if has_alerts:
                logins = {row["comment_hash"]: row["login"] for row in conn.execute(
                    "SELECT comment_hash, login FROM candidate_alerts"
                )}
            archived = set()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='panel_archived_comments'").fetchone():
                archived = {row[0] for row in conn.execute("SELECT comment_hash FROM panel_archived_comments")}
            members = []
            for row in conn.execute(
                "SELECT profile_id, score, date_text, comment, source_url, updated_at "
                "FROM candidates WHERE score >= 8 ORDER BY score DESC, updated_at DESC"
            ):
                item = dict(row)
                if not is_acceptable_comment(item["comment"]):
                    continue
                item["score"] = score_comment(item["comment"])
                if item["score"] < 8:
                    continue
                digest = hashlib.sha256(item["comment"].encode("utf-8")).hexdigest()
                if digest in archived:
                    continue
                item["login"] = logins.get(digest)
                if not item["source_url"].startswith(("https://", "http://")):
                    item["source_url"] = None
                members.append(item)
            return {"members": members, "error": None}
    except sqlite3.Error:
        return {"members": [], "error": "Não foi possível carregar os comentários. Atualize a página para tentar novamente."}
