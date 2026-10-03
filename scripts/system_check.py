from __future__ import annotations
import argparse, asyncio, sqlite3, sys, urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings
from app.services import batch_status


def result(level, name, detail):
    print(f"{level:<5} {name}: {detail}")


def db_checks():
    conn=sqlite3.connect(settings.database_path); conn.row_factory=sqlite3.Row
    integrity=conn.execute('pragma integrity_check').fetchone()[0]
    result('OK' if integrity=='ok' else 'ERRO','database_integrity',integrity)
    rows=conn.execute('select * from members order by id').fetchall()
    result('OK','members',str(len(rows)))
    dup_u=conn.execute("select count(*) from (select username from members where username is not null group by username having count(*)>1)").fetchone()[0]
    dup_e=conn.execute("select count(*) from (select email from members where email is not null group by email having count(*)>1)").fetchone()[0]
    result('OK' if dup_u==0 else 'ERRO','duplicate_usernames',str(dup_u))
    result('OK' if dup_e==0 else 'ERRO','duplicate_emails',str(dup_e))
    impossible=[]
    for r in rows:
        if r['registration_status']=='PROFILE_COMPLETE' and (r['email_status']!='CONFIRMED' or r['profile_status']!='COMPLETE'): impossible.append(r['id'])
        if r['profile_status']=='COMPLETE' and r['email_status']!='CONFIRMED': impossible.append(r['id'])
    result('OK' if not impossible else 'ERRO','state_consistency',f"inconsistentes={len(set(impossible))}")
    counts=Counter(r['registration_status'] for r in rows)
    result('OK','registration_states',str(dict(counts)))
    conn.close()


def health_check():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3) as r:
            body=r.read().decode('utf-8','replace')
        result('OK','manager_health',body)
    except Exception as e:
        result('ERRO','manager_health',type(e).__name__)


async def browser_check():
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            # The smoke test runs from shells without a graphical display.
            b=await p.chromium.launch(headless=True)
            pg=await b.new_page(); await pg.set_content('<h1>ok</h1>')
            ok=(await pg.locator('h1').inner_text())=='ok'; await b.close()
        result('OK' if ok else 'ERRO','browser_smoke','local page only; no external site')
    except Exception as e:
        result('ERRO','browser_smoke',f"{type(e).__name__}: {e}")


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--browser',action='store_true'); args=ap.parse_args()
    st=batch_status()
    result('OK','registration_total_limit',str(st['total_limit']))
    result('OK','direct_limit',str(st['direct_limit']))
    result('OK','vpn_daily_limit',str(st['vpn_limit']))
    result('OK' if st['vpn_ready'] else 'AVISO','vpn_ready',str(st['vpn_ready']))
    result('OK','batch_trigger','manual')
    result('OK','batch_available',str(st['available']))
    result('OK','available_capacity',str(st['available_capacity']))
    result('OK','route_capacity',f"direct={st['direct_capacity']}; vpn={st['vpn_capacity']}")
    env_mode=oct((ROOT/'.env').stat().st_mode & 0o777)
    result('OK' if env_mode in ('0o600','0o640') else 'AVISO','env_permissions',env_mode)
    db_checks(); health_check()
    if args.browser: asyncio.run(browser_check())

if __name__=='__main__': main()
