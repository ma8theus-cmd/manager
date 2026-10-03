from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/home/matheus/meslibertines_manager_v1')
RESEARCH = ROOT / 'research'
DB_PATH = RESEARCH / 'comment_research.db'
MAIN_DB = ROOT / 'data' / 'members.db'
SITEMAP_CACHE = RESEARCH / 'sitemap_alt.xml'
STATUS_JSON = RESEARCH / 'status.json'
EXPORT_JSON = RESEARCH / 'qualified_comments.json'
EXPORT_CSV = RESEARCH / 'qualified_comments.csv'
EXPORT_TXT = RESEARCH / 'qualified_comments.txt'
TARGET = 50
MIN_SCORE = 8.0
USER_AGENT = 'MesLibertinesResearch/1.0 (public pages; rate-limit respectful)'
BASE = 'https://www.meslibertines.com'
load_dotenv(ROOT / '.env')
NODE_ROLE = os.getenv('MESLIB_NODE_ROLE', 'all').strip().lower() or 'all'


def effective_target(value: int) -> int:
    return max(TARGET, min(1000, int(value)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec='seconds')


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    RESEARCH.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            section_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL,
            checked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_research_queue_status ON queue(status,id);
        ''')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS candidates(
            profile_hash TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL,
            date_text TEXT,
            comment TEXT NOT NULL,
            source_url TEXT NOT NULL,
            section_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_comments(
            comment_hash TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_alerts(
            comment_hash TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            score REAL NOT NULL,
            source_url TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL
        );
        ''')


def get_state(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        'INSERT INTO state(key,value) VALUES(?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value),
    )

def log_event(conn: sqlite3.Connection, level: str, message: str) -> None:
    conn.execute(
        'INSERT INTO events(created_at,level,message) VALUES(?,?,?)',
        (iso(), level, message[:1000]),
    )
    print(f'{level}: {message}', flush=True)


def main_site_busy(node_role: str | None = None) -> tuple[bool, str]:
    if not MAIN_DB.exists():
        return False, ''
    role = (node_role or NODE_ROLE).strip().lower() or 'all'
    conn = sqlite3.connect(MAIN_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        active_scout = conn.execute(
            "SELECT COUNT(*) n FROM comment_scouts WHERE status='ACTIVE'"
        ).fetchone()['n']
        awaiting = conn.execute(
            "SELECT COUNT(*) n FROM comment_bank WHERE status IN ('POSTING','AWAITING_CONFIRMATION')"
        ).fetchone()['n']
        active_reg = conn.execute(
            "SELECT COUNT(*) n FROM members WHERE registration_status='FORM_PREPARING' OR profile_status='ACTIVATING'"
        ).fetchone()['n']
        # On VPS1/control, comment posting and Scout live on VPS2 and must not
        # block public research. During migration (role=all), preserve the old guard.
        if role == 'control':
            if active_reg:
                return True, f'operacao ativa: cadastros={active_reg}'
            recent = conn.execute(
                "SELECT MAX(account_created_at) ts FROM members WHERE account_created_at IS NOT NULL"
            ).fetchone()['ts']
        else:
            if active_scout or awaiting or active_reg:
                return True, f'operacao ativa: scouts={active_scout}, comentarios={awaiting}, cadastros={active_reg}'
            recent = conn.execute(
                "SELECT MAX(ts) ts FROM ("
                "SELECT account_created_at ts FROM members WHERE account_created_at IS NOT NULL "
                "UNION ALL SELECT submitted_at ts FROM comment_bank WHERE submitted_at IS NOT NULL)"
            ).fetchone()['ts']
    finally:
        conn.close()
    return False, ''


def fetch(url: str, timeout: int = 25) -> tuple[int, str, dict[str, str]]:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            return resp.status, body, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers else {}
        body = exc.read().decode('utf-8', 'replace') if exc.fp else ''
        return exc.code, body, headers
    except Exception as exc:
        return 0, f'{type(exc).__name__}: {exc}', {}


def strip_tags(value: str) -> str:
    value = re.sub(r'<br\s*/?>', ' ', value, flags=re.I)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = html.unescape(value)
    return re.sub(r'\s+', ' ', value).strip()

DETAIL_GROUPS = [
    ('photo', 'réelle', 'reelle', 'conforme'),
    ('rdv', 'rendez-vous', 'rendez vous', 'ponctuel', 'ponctuelle'),
    ('accueil', 'souriante', 'sympathique', 'respectueuse', 'attentionnée'),
    ('massage', 'ambiance', 'conversation', 'hygiène', 'hygiene'),
    ('prestation', 'service', 'qualité', 'qualite', 'soin'),
    ('recommande', 'recommand', 'revoir', 'retourner', 'deuxième', 'deuxieme'),
]

# These terms describe advertising/contact copy rather than a service review.
PROMO_TERMS = (
    'telegram', 'whatsapp', 'contact*', 'http://', 'https://', '@',
    'disponib*', 'reserv*', 'tarif*', 'promo*', 'annonce*', 'offre*',
)

# A single occurrence rejects the whole comment. This keeps mixed reviews out
# instead of trying to edit or sanitize the member's original words.
REJECT_TERMS = (
    'sexuel*', 'erot*', 'prelim*', 'fellation*', 'sucer*', 'suce*',
    'chatte*', 'bite*', 'penis*', 'jouet*', 'gfe', 'pse',
    'orgasm*', 'ejac*', 'sodom*', 'anal*', 'cuni*', 'pipe',
    'blowjob', 'facefuck', 'rapport sexuel', 'faire l amour',
    'nud*', 'deshab*', 'gorge profonde', 'penetr*',
)

# At least one substantive service/professional criterion is required.
# Recommendation/revisit words alone are not enough.
VALID_CORE_TERMS = (
    'massage*', 'profession*', 'respect*', 'accueil*', 'souri*',
    'sympath*', 'gentil*', 'agreable*', 'educ*', 'courtois*', 'poli*',
    'ecoute*', 'attention*', 'bienveill*', 'ponctu*', 'fiabl*',
    'discret*', 'ambiance*', 'cadre', 'environnement', 'propre*',
    'hygien*', 'qualit*', 'service*', 'prestation*', 'soin*',
    'savoir-faire', 'talent*', 'serieux*', 'calme', 'detent*',
    'confort*', 'bonne humeur', 'conversation', 'discut*', 'dialogue',
    'respecte*', 'engagement*', 'promesse*', 'duree', 'horaire',
    'mettre a l aise', 'a l ecoute',
)
VALID_TERMS = VALID_CORE_TERMS + (
    'recommand*', 'revoir', 'retour*', 'deuxieme',
)


def _normalize_filter_text(text: str) -> str:
    value = unicodedata.normalize('NFKD', text or '').casefold()
    return ''.join(char for char in value if not unicodedata.combining(char))


def _term_pattern(term: str) -> str:
    prefix = term.endswith('*')
    core = term[:-1] if prefix else term
    escaped = re.escape(_normalize_filter_text(core)).replace(r'\ ', r'\s+')
    suffix = r'\w*' if prefix else r'(?!\w)'
    return rf'(?<!\w){escaped}{suffix}'


def _contains_term(value: str, term: str) -> bool:
    if term in {'@', 'http://', 'https://'}:
        return term in value
    return re.search(_term_pattern(term), value) is not None


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(_contains_term(value, term) for term in terms)


def is_acceptable_comment(text: str) -> bool:
    """Keep only authentic-looking professional feedback.

    The original wording is never rewritten: a mixed comment is rejected as a
    whole when it contains sexual, suggestive, advertising, or appearance-only
    content.
    """
    normalized = re.sub(r'\s+', ' ', _normalize_filter_text(text)).strip()
    if not normalized:
        return False
    if _contains_any(normalized, PROMO_TERMS):
        return False
    if _contains_any(normalized, REJECT_TERMS):
        return False
    return _contains_any(normalized, VALID_CORE_TERMS)


def score_comment(text: str) -> float:
    if not is_acceptable_comment(text):
        return 0.0

    normalized = re.sub(r'\s+', ' ', _normalize_filter_text(text)).strip()
    score = 6.4
    n = len(normalized)
    if 80 <= n < 220:
        score += 0.6
    elif 220 <= n < 650:
        score += 1.0
    elif n >= 650:
        score += 0.8

    matched = sum(1 for term in VALID_TERMS if _contains_term(normalized, term))
    score += min(3.0, matched * 0.4)

    matched_groups = sum(
        1 for group in DETAIL_GROUPS if _contains_any(normalized, group)
    )
    score += min(0.6, matched_groups * 0.2)

    if re.search(r'(?<!\w)(je|j|nous)(?!\w)', normalized):
        score += 0.2
    if _contains_any(normalized, ('revoir', 'retour*', 'deuxieme')):
        score += 0.3
    if normalized.count('.') + normalized.count('!') + normalized.count('?') >= 2:
        score += 0.2

    letters = [char for char in normalized if char.isalpha()]
    if letters:
        upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
        if upper_ratio > 0.35:
            score -= 1.0
    return round(max(0.0, min(10.0, score)), 1)


def parse_comments(body: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r'<div id="cm_(\d+)".*?<div class="info">\s*par\s*<span>\s*'
        r'<a href="(/member/[^"]+)">(.*?)</a>\s*</span><br\s*/?>\s*'
        r'([^<]+)<br\s*/?>\s*</div>\s*<p>(.*?)</p>',
        re.I | re.S,
    )
    rows = []
    for match in pattern.finditer(body):
        comment_id, member_href, login, date_text, raw_comment = match.groups()
        comment = strip_tags(raw_comment)
        if not comment:
            continue
        rows.append({
            'comment_id': comment_id,
            'member_href': member_href.lower(),
            'login': strip_tags(login),
            'date_text': strip_tags(date_text),
            'comment': comment,
        })
    return rows


def pager_urls(body: str, section_id: str) -> list[str]:
    pattern = rf'href="(/comments/index/1/section/profiles/section_id/{re.escape(section_id)}/page\d+\.html)"'
    return sorted({BASE + html.unescape(x) for x in re.findall(pattern, body, flags=re.I)})

def ensure_sitemap(conn: sqlite3.Connection) -> bool:
    stale = True
    if SITEMAP_CACHE.exists():
        age = utc_now().timestamp() - SITEMAP_CACHE.stat().st_mtime
        stale = age > 7 * 24 * 3600
    if not stale:
        return True

    status, body, _ = fetch(BASE + '/sitemap_alt.xml', timeout=40)
    if status != 200 or '<urlset' not in body:
        log_event(conn, 'WARN', f'sitemap indisponivel: HTTP {status}')
        return SITEMAP_CACHE.exists()
    SITEMAP_CACHE.write_text(body, encoding='utf-8')
    log_event(conn, 'INFO', f'sitemap atualizado ({len(body)} bytes)')
    return True


def seed_queue(conn: sqlite3.Connection) -> int:
    pending = conn.execute("SELECT COUNT(*) n FROM queue WHERE status='PENDING'").fetchone()['n']
    if pending >= 500:
        return 0
    if not ensure_sitemap(conn):
        return 0

    text = SITEMAP_CACHE.read_text(encoding='utf-8', errors='replace')
    ids = []
    seen = set()
    for match in re.finditer(r'<loc>https://www\.meslibertines\.com/escort/[^<]*?-(\d+)/</loc>', text):
        sid = match.group(1)
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)

    cursor = int(get_state(conn, 'sitemap_cursor', '0') or 0)
    if cursor >= len(ids):
        cursor = 0
    added = 0
    for sid in ids[cursor:cursor + 1000]:
        url = f'{BASE}/comments/index/1/section/profiles/section_id/{sid}/'
        cur = conn.execute(
            "INSERT OR IGNORE INTO queue(url,section_id,status,added_at) VALUES(?,?,'PENDING',?)",
            (url, sid, iso()),
        )
        added += cur.rowcount
    set_state(conn, 'sitemap_cursor', str(min(len(ids), cursor + 1000)))
    return added

def save_candidate(conn: sqlite3.Connection, row: dict[str, str], score: float, source_url: str, section_id: str) -> bool:
    profile_hash = hashlib.sha256(row['member_href'].encode('utf-8')).hexdigest()
    existing = conn.execute(
        'SELECT profile_id,score FROM candidates WHERE profile_hash=?',
        (profile_hash,),
    ).fetchone()
    if existing and existing['score'] >= score:
        return False

    if existing:
        profile_id = existing['profile_id']
    else:
        next_no = conn.execute("SELECT COALESCE(MAX(CAST(SUBSTR(profile_id,2) AS INTEGER)),0) n FROM candidates").fetchone()['n'] + 1
        profile_id = f'P{next_no:03d}'

    conn.execute(
        '''INSERT INTO candidates(profile_hash,profile_id,score,date_text,comment,source_url,section_id,updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(profile_hash) DO UPDATE SET
             score=excluded.score,date_text=excluded.date_text,comment=excluded.comment,
             source_url=excluded.source_url,section_id=excluded.section_id,updated_at=excluded.updated_at''',
        (profile_hash, profile_id, score, row['date_text'], row['comment'], source_url, section_id, iso()),
    )
    return True


def eligible_candidate_rows(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    rows = []
    for row in conn.execute(
        'SELECT profile_id,score,date_text,comment,source_url,section_id '
        'FROM candidates ORDER BY score DESC, profile_id'
    ):
        item = dict(row)
        if not is_acceptable_comment(item['comment']):
            continue
        score = score_comment(item['comment'])
        if score < MIN_SCORE:
            continue
        item['score'] = score
        rows.append(item)
    rows.sort(key=lambda item: (-item['score'], item['profile_id']))
    return rows if limit is None else rows[:limit]


def format_candidate_alert(row: dict, score: float, source_url: str) -> str:
    login = row['login'].replace('@', '@\u200b').replace('\x60', '')
    return (
        "🔎 **MEMBRO PROMISSOR ENCONTRADO**\n"
        f"Login: {login}\nNota do comentário: {score:.1f}/10\n"
        f"Fonte: {source_url}"
    )


def send_pending_alerts(conn: sqlite3.Connection) -> None:
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        return
    rows = conn.execute(
        "SELECT * FROM candidate_alerts WHERE sent_at IS NULL AND score >= ? ORDER BY rowid LIMIT 20", (MIN_SCORE,)
    ).fetchall()
    for row in rows:
        message = format_candidate_alert(dict(row), row['score'], row['source_url'])
        payload = json.dumps({
            'content': message, 'allowed_mentions': {'parse': []}
        }).encode('utf-8')
        req = urllib.request.Request(
            webhook, data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                response.read()
            conn.execute(
                'UPDATE candidate_alerts SET sent_at=? WHERE comment_hash=?',
                (iso(), row['comment_hash']),
            )
            conn.commit()
        except Exception as exc:
            log_event(conn, 'WARN', f'alerta Discord pendente: {type(exc).__name__}')
            conn.commit()
            break


def export_results(conn: sqlite3.Connection) -> int:
    eligible = eligible_candidate_rows(conn)
    rows = eligible[:TARGET]
    EXPORT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    with EXPORT_CSV.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['profile_id','score','date_text','comment','source_url','section_id'])
        writer.writeheader()
        writer.writerows(rows)
    if len(eligible) >= TARGET:
        parts = []
        for r in rows:
            parts.append(f"{r['profile_id']} | nota {r['score']:.1f}/10 | {r.get('date_text') or 'data não disponível'}\n{r['comment']}\nFonte: {r['source_url']}\n")
        EXPORT_TXT.write_text('\n'.join(parts), encoding='utf-8')
    elif EXPORT_TXT.exists():
        # Never leave an old partial or unfiltered TXT available.
        EXPORT_TXT.unlink()
    conn.commit()
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.lead_batches import generate_batches, notify_ready_batches
    generate_batches(DB_PATH, RESEARCH / 'lead_batches')
    notify_ready_batches(DB_PATH, os.getenv("DISCORD_WEBHOOK_URL", "").strip())
    return len(eligible)

def write_status(conn: sqlite3.Connection, note: str = '', target: int = TARGET) -> dict:
    qualified = len(eligible_candidate_rows(conn))
    pending = conn.execute("SELECT COUNT(*) n FROM queue WHERE status='PENDING'").fetchone()['n']
    done = conn.execute("SELECT COUNT(*) n FROM queue WHERE status='DONE'").fetchone()['n']
    errors = conn.execute("SELECT COUNT(*) n FROM queue WHERE status='ERROR'").fetchone()['n']
    data = {
        'updated_at': iso(),
        'target': target,
        'min_score': MIN_SCORE,
        'qualified': qualified,
        'pending_pages': pending,
        'processed_pages': done,
        'error_pages': errors,
        'backoff_until': get_state(conn, 'backoff_until', ''),
        'note': note,
        'complete': qualified >= target,
    }
    STATUS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def process_page(conn: sqlite3.Connection, qrow: sqlite3.Row) -> tuple[bool, str]:
    status, body, headers = fetch(qrow['url'])
    conn.execute(
        'UPDATE queue SET attempts=attempts+1, checked_at=? WHERE id=?',
        (iso(), qrow['id']),
    )

    if status == 429:
        retry = headers.get('Retry-After', '').strip()
        seconds = int(retry) if retry.isdigit() else 6 * 3600
        until = utc_now() + timedelta(seconds=max(1800, seconds))
        set_state(conn, 'backoff_until', iso(until))
        log_event(conn, 'WARN', f'HTTP 429; pausa ate {iso(until)}')
        return False, 'RATE_LIMIT'
    if status != 200:
        new_status = 'ERROR' if qrow['attempts'] >= 2 else 'PENDING'
        conn.execute('UPDATE queue SET status=? WHERE id=?', (new_status, qrow['id']))
        log_event(conn, 'WARN', f'HTTP {status} em {qrow["url"]}')
        return True, f'HTTP_{status}'

    comments = parse_comments(body)
    new_qualified = 0
    for row in comments:
        chash = hashlib.sha256(row['comment'].encode('utf-8')).hexdigest()
        # Seeded exploratory results start with an anonymous placeholder hash.
        # When the exact public comment is encountered again, bind it to the
        # real public member href before the seen-comment short circuit.
        actual_profile_hash = hashlib.sha256(row['member_href'].encode('utf-8')).hexdigest()
        seeded = conn.execute(
            'SELECT profile_hash,profile_id FROM candidates WHERE comment=? LIMIT 1',
            (row['comment'],),
        ).fetchone()
        if seeded and seeded['profile_hash'] != actual_profile_hash:
            clash = conn.execute(
                'SELECT 1 FROM candidates WHERE profile_hash=?', (actual_profile_hash,)
            ).fetchone()
            if not clash:
                conn.execute(
                    'UPDATE candidates SET profile_hash=?,source_url=?,section_id=?,updated_at=? WHERE profile_id=?',
                    (actual_profile_hash, qrow['url'], qrow['section_id'], iso(), seeded['profile_id']),
                )
        if conn.execute('SELECT 1 FROM seen_comments WHERE comment_hash=?', (chash,)).fetchone():
            continue
        conn.execute(
            'INSERT INTO seen_comments(comment_hash,seen_at) VALUES(?,?)',
            (chash, iso()),
        )
        if not is_acceptable_comment(row['comment']):
            log_event(conn, 'INFO', f'comentário rejeitado pelo filtro de conteúdo: {qrow["section_id"]}')
            continue
        score = score_comment(row['comment'])
        if score >= MIN_SCORE:
            if save_candidate(conn, row, score, qrow['url'], qrow['section_id']):
                new_qualified += 1
                conn.execute(
                    'INSERT OR IGNORE INTO candidate_alerts(comment_hash,login,score,source_url) VALUES(?,?,?,?)',
                    (chash, row['login'], score, qrow['url']),
                )
                conn.execute(
                    'UPDATE candidate_alerts SET login=?,score=? WHERE comment_hash=? AND sent_at IS NULL',
                    (row['login'], score, chash),
                )

    for url in pager_urls(body, qrow['section_id']):
        conn.execute(
            "INSERT OR IGNORE INTO queue(url,section_id,status,added_at) VALUES(?,?,'PENDING',?)",
            (url, qrow['section_id'], iso()),
        )
    conn.execute("UPDATE queue SET status='DONE' WHERE id=?", (qrow['id'],))
    set_state(conn, 'backoff_until', '')
    log_event(
        conn,
        'INFO',
        f'pagina processada: comentarios={len(comments)}, novos>={MIN_SCORE:g}={new_qualified}, section={qrow["section_id"]}',
    )
    return True, 'OK'


def backoff_active(conn: sqlite3.Connection) -> tuple[bool, str]:
    raw = get_state(conn, 'backoff_until', '')
    if not raw:
        return False, ''
    try:
        until = datetime.fromisoformat(raw)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if utc_now() < until:
            return True, raw
    except ValueError:
        pass
    set_state(conn, 'backoff_until', '')
    return False, ''

def run(max_pages: int, target: int = TARGET, node_role: str | None = None) -> int:
    init_db()
    with connect() as conn:
        send_pending_alerts(conn)
        current = export_results(conn)
        if current >= target:
            write_status(conn, 'alvo concluido; nenhuma nova requisicao', target=target)
            print(f'COMPLETE {current}/{target}', flush=True)
            return 0

        busy, reason = main_site_busy(node_role)
        if busy:
            write_status(conn, 'pausado: ' + reason, target=target)
            print('PAUSED', reason, flush=True)
            return 0

        blocked, until = backoff_active(conn)
        if blocked:
            write_status(conn, f'backoff HTTP ate {until}', target=target)
            print('BACKOFF', until, flush=True)
            return 0

        added = seed_queue(conn)
        conn.commit()
        if added:
            print(f'QUEUE_SEEDED +{added}', flush=True)

        processed = 0
        while processed < max_pages:
            if len(eligible_candidate_rows(conn)) >= target:
                break
            qrow = conn.execute(
                "SELECT * FROM queue WHERE status='PENDING' ORDER BY id LIMIT 1"
            ).fetchone()
            if not qrow:
                seed_queue(conn)
                conn.commit()
                qrow = conn.execute(
                    "SELECT * FROM queue WHERE status='PENDING' ORDER BY id LIMIT 1"
                ).fetchone()
                if not qrow:
                    break
            should_continue, result = process_page(conn, qrow)
            conn.commit()
            send_pending_alerts(conn)
            processed += 1
            if result == 'RATE_LIMIT' or not should_continue:
                break
            if processed < max_pages:
                time.sleep(5)

        count = export_results(conn)
        note = f'rodada concluida: paginas={processed}, qualificados={count}/{target}'
        status = write_status(conn, note, target=target)
        log_event(conn, 'INFO', note)
        conn.commit()
        print(json.dumps(status, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument(
        '--target-total', type=int, default=TARGET,
        help='continua a coleta até atingir este total de leads elegíveis',
    )
    parser.add_argument(
        '--node-role', choices=('all', 'control', 'comment_worker'), default=None,
        help='papel desta execução; control não bloqueia Scouts/comentários ativos',
    )
    args = parser.parse_args()
    max_pages = max(1, min(20, args.max_pages))
    target = effective_target(args.target_total)
    return run(max_pages, target=target, node_role=args.node_role)


if __name__ == '__main__':
    raise SystemExit(main())
