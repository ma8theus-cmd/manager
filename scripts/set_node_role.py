from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.env'
ALLOWED = {'all', 'control', 'comment_worker'}

if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED:
    raise SystemExit('uso: set_node_role.py all|control|comment_worker')

role = sys.argv[1]
lines = ENV.read_text(encoding='utf-8').splitlines() if ENV.exists() else []
out = []
changed = False
for line in lines:
    if line.startswith('MESLIB_NODE_ROLE='):
        out.append(f'MESLIB_NODE_ROLE={role}')
        changed = True
    else:
        out.append(line)
if not changed:
    out.append(f'MESLIB_NODE_ROLE={role}')
ENV.write_text('\n'.join(out) + '\n', encoding='utf-8')
print(role)
