from __future__ import annotations
import argparse, sqlite3, sys
from datetime import date, datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from app.config import settings
from app.services import batch_status
from app.username_generator import username_candidates, is_valid_username


def age_on(dob: date, today: date) -> int:
    return today.year-dob.year-((today.month,today.day)<(dob.month,dob.day))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--ignore-window',action='store_true'); args=ap.parse_args()
    st=batch_status(); problems=[]
    if not st['available'] and not args.ignore_window: problems.append('nenhuma rota de cadastro com capacidade disponível')
    c=sqlite3.connect(settings.database_path); c.row_factory=sqlite3.Row
    amount=st['available_capacity'] if not args.ignore_window else min(st['pending'], st['total_limit'])
    rows=c.execute("select * from members where registration_status='PENDING' order by id limit ?",(amount,)).fetchall()
    if amount and len(rows)<amount: problems.append(f'apenas {len(rows)} pendentes disponíveis para capacidade {amount}')
    print('PREVIEW_IDS=',[r['id'] for r in rows])
    for r in rows:
        for field in ('first_name','last_name','birth_date','city_france'):
            if not (r[field] or '').strip(): problems.append(f"#{r['id']} sem {field}")
        try:
            dob=datetime.fromisoformat(r['birth_date']).date()
            if age_on(dob,date.today())<18: problems.append(f"#{r['id']} menor de 18")
        except Exception: problems.append(f"#{r['id']} birth_date inválida")
        if r['username'] and not is_valid_username(r['username']): problems.append(f"#{r['id']} username inválido")
        if not r['username']:
            next(username_candidates(r['first_name'],r['last_name'],seed=r['id']))
    c.close()
    if problems:
        print('PREFLIGHT=BLOCKED'); [print(' -',x) for x in problems]
        raise SystemExit(2)
    print('PREFLIGHT=READY')

if __name__=='__main__': main()
