"""Persistent, non-overlapping batches of 50 unarchived lead comments."""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from research.comment_research_collector import is_acceptable_comment, score_comment

BATCH_SIZE = 50

def _pending(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    archived = {r[0] for r in conn.execute("SELECT comment_hash FROM panel_archived_comments")} if "panel_archived_comments" in tables else set()
    exported = {r[0] for r in conn.execute("SELECT comment_hash FROM lead_batch_items")} if "lead_batch_items" in tables else set()
    logins = {r["comment_hash"]: r["login"] for r in conn.execute("SELECT comment_hash,login FROM candidate_alerts")} if "candidate_alerts" in tables else {}
    result = []
    for row in conn.execute("SELECT profile_id,score,date_text,comment,source_url FROM candidates WHERE score>=8 ORDER BY updated_at,profile_id"):
        item = dict(row)
        if not is_acceptable_comment(item["comment"]):
            continue
        item["score"] = score_comment(item["comment"])
        if item["score"] < 8:
            continue
        digest = hashlib.sha256(item["comment"].encode("utf-8")).hexdigest()
        if digest in archived or digest in exported:
            continue
        exported.add(digest)
        item.update(comment_hash=digest, login=logins.get(digest))
        result.append(item)
    return result


def _eligible_hashes(conn):
    hashes = set()
    for row in conn.execute("SELECT comment FROM candidates WHERE score>=8"):
        comment = row[0]
        if is_acceptable_comment(comment) and score_comment(comment) >= 8:
            hashes.add(hashlib.sha256(comment.encode("utf-8")).hexdigest())
    return hashes


def _batch_is_clean(conn, batch_id, eligible_hashes=None):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "lead_batch_items" not in tables:
        return False
    items = conn.execute(
        "SELECT comment_hash FROM lead_batch_items WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    if len(items) != BATCH_SIZE:
        return False
    allowed = eligible_hashes if eligible_hashes is not None else _eligible_hashes(conn)
    return all(row[0] in allowed for row in items)


def _render(number, rows):
    parts = [f"LOTE {number:03d} — 50 COMENTÁRIOS — NOTA MÍNIMA 8/10\n"]
    for i, r in enumerate(rows, 1):
        parts.append(f"{i}. {r['login'] or r['profile_id']} | Nota: {r['score']:.1f}/10\nData: {r['date_text'] or 'Não disponível'}\n{r['comment']}\nFonte: {r['source_url']}\n")
    return "\n".join(parts)

def generate_batches(db_path, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS lead_txt_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, content TEXT NOT NULL, item_count INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS lead_batch_items (comment_hash TEXT PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES lead_txt_batches(id))")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(lead_txt_batches)")}
        if "notified_at" not in columns:
            conn.execute("ALTER TABLE lead_txt_batches ADD COLUMN notified_at TEXT")
        rows = _pending(conn)
        created = []
        for start in range(0, len(rows) - BATCH_SIZE + 1, BATCH_SIZE):
            chunk = rows[start:start+BATCH_SIZE]
            cur = conn.execute("INSERT INTO lead_txt_batches(created_at,content,item_count) VALUES (?,?,?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"), "", BATCH_SIZE))
            number = cur.lastrowid
            conn.execute("UPDATE lead_txt_batches SET content=? WHERE id=?", (_render(number, chunk), number))
            conn.executemany("INSERT INTO lead_batch_items(comment_hash,batch_id) VALUES (?,?)", [(r["comment_hash"], number) for r in chunk])
            created.append(number)
        conn.commit()
        # Persist the snapshot first. Interrupted file writes are repaired next run.
        for row in conn.execute("SELECT id,content FROM lead_txt_batches ORDER BY id"):
            path = directory / f"leads_lote_{row['id']:03d}.txt"
            expected = row["content"].encode("utf-8-sig")
            if path.exists() and path.read_bytes() == expected:
                continue
            temp = directory / f".{path.name}.{os.getpid()}.tmp"
            temp.write_bytes(expected)
            os.replace(temp, path)
        return created

def batch_dashboard_data(db_path):
    if not Path(db_path).exists():
        return {"batches": [], "pending": 0}
    with sqlite3.connect(Path(db_path).as_uri()+"?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='lead_txt_batches'").fetchone()
        if exists:
            eligible_hashes = _eligible_hashes(conn)
            batches = [
                dict(row) for row in conn.execute(
                    "SELECT id,created_at,item_count FROM lead_txt_batches ORDER BY id DESC"
                )
                if _batch_is_clean(conn, row["id"], eligible_hashes)
            ]
        else:
            batches = []
        return {"batches": batches, "pending": len(_pending(conn))}

def batch_content(db_path, batch_id):
    with sqlite3.connect(Path(db_path).as_uri()+"?mode=ro", uri=True, timeout=5) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lead_txt_batches'").fetchone():
            return None
        row = conn.execute("SELECT content FROM lead_txt_batches WHERE id=?", (batch_id,)).fetchone()
        if not row or not _batch_is_clean(conn, batch_id):
            return None
        return row[0]

def notify_ready_batches(db_path, webhook):
    if not webhook:
        return
    import json
    import urllib.request
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,item_count FROM lead_txt_batches WHERE notified_at IS NULL ORDER BY id").fetchall()
        for row in rows:
            payload = json.dumps({"content": f"📄 **TXT DE LEADS PRONTO**\nLote {row['id']:03d}: {row['item_count']} comentários com nota 8 ou superior.\nBaixe no painel, em **Membros promissores → Baixar TXT · Lote {row['id']}**.\nArquivo: leads_lote_{row['id']:03d}.txt", "allowed_mentions": {"parse": []}}).encode("utf-8")
            request = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json", "User-Agent": "MesLibertinesResearch/1.0"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read()
                conn.execute("UPDATE lead_txt_batches SET notified_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]))
                conn.commit()
            except Exception as exc:
                print(f"TXT lote {row['id']}: aviso pendente ({type(exc).__name__}); nova tentativa na próxima rodada.", flush=True)
                break
