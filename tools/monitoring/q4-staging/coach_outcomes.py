"""Read existing, voluntarily uploaded Coach outcomes; never expose keys or game payloads."""
import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def read_snapshot(path):
    """Copy a stable DB/WAL set into private temporary storage before reading.

    A live writer can change any of the files between copies; retry rather than
    serving a partial snapshot. No files are created in the read-only mount.
    """
    path=Path(path)
    def signature():
        return tuple((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')) if p.exists())
    for _ in range(4):
        before=signature()
        if not before:raise RuntimeError('Coach outcome database is unavailable')
        with tempfile.TemporaryDirectory(prefix='coach-outcomes-') as tmp:
            target=Path(tmp)/'snapshot.db'
            for name,_,_ in before:
                shutil.copyfile(name,str(target)+name[len(str(path)):])
            if signature()!=before:continue
            conn=sqlite3.connect(str(target));conn.row_factory=sqlite3.Row
            try:
                if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':continue
                columns = {r[1] for r in conn.execute('PRAGMA table_info(match_bundles)')}
                extra = ','.join(name if name in columns else 'NULL AS ' + name
                                 for name in ('autopilot_enabled', 'format_name', 'deck_name'))
                bundles=[dict(r) for r in conn.execute('SELECT owner,install_id,match_id,received_at,result,platform,' + extra + ' FROM match_bundles')]
                subscribers=[dict(r) for r in conn.execute('SELECT license_key,email,name FROM subscribers')]
                trials=[dict(r) for r in conn.execute('SELECT litellm_key,email FROM trials')]
                return bundles,subscribers,trials
            finally:conn.close()
    raise RuntimeError('Coach outcome snapshot changed during read; retry shortly')


def _dimensions(entries):
    # Never infer manual play from missing metadata. Conflicting or incomplete
    # duplicate reports have unknown dimensions rather than counting twice.
    values = []
    for name in ('autopilot_enabled', 'format_name', 'deck_name'):
        normalized = []
        for bundle, _ in entries:
            value = bundle.get(name)
            if name == 'autopilot_enabled':
                value = ('autopilot' if value == 1 else 'played by hand') if type(value) in (bool, int) and value in (0, 1) else 'unknown'
            else:
                value = value.strip()[:160] if isinstance(value, str) and value.strip() else 'unknown'
            normalized.append(value)
        unique = set(normalized)
        values.append(next(iter(unique)) if len(unique) == 1 else 'unknown')
    return dict(zip(('play_mode', 'format_name', 'deck_name'), values))


def aggregate(bundles, identities, frm, to, dimensions=()):
    grouped=defaultdict(list)
    # Deduplicate after identity resolution, including the same game reported
    # from two installations or from rotated keys on a known account.
    for b in bundles:
        ident=identities.get(b['owner'],{'id':'uploader:'+b['owner'],'user':'Uploader '+b['owner'][:8],'kind':'unmatched'})
        grouped[(ident['id'],b['match_id'])].append((b,ident))
    users={}
    for entries in grouped.values():
        # Use the first known outcome, so an earlier partial report does not
        # suppress a later completed upload. Conflicting outcomes are excluded.
        known=[e for e in entries if str(e[0].get('result','')).lower() in ('win','loss','draw')]
        selected=min(known or entries,key=lambda e:e[0]['received_at']);b,ident=selected
        received=b['received_at']
        if not frm<=received<to:continue
        metadata = _dimensions(entries)
        group = (ident['id'],) + tuple(metadata[name] for name in dimensions)
        r=users.setdefault(group,{'user':ident['user'],'access':ident['kind'],'wins':0,'losses':0,'draws':0,'unknown':0,'conflicts':0,'games':0,'platforms':set(),'last_report_epoch':0})
        r.update({name: metadata[name] for name in dimensions})
        results={str(e[0].get('result','')).lower() for e in known}
        outcome=str(b.get('result','')).lower()
        if len(results)>1:r['conflicts']+=1;r['unknown']+=1
        elif outcome in ('win','loss','draw'):r[{'win':'wins','loss':'losses','draw':'draws'}[outcome]]+=1
        else:r['unknown']+=1
        r['games']+=1
        r['platforms'].update(str(e[0].get('platform') or 'unknown') for e in entries)
        r['last_report_epoch']=max(r['last_report_epoch'],received)
    rows=[]
    for r in users.values():
        r['decided_games']=r['wins']+r['losses']
        r['win_rate_pct']=round(100*r['wins']/r['decided_games'],1) if r['decided_games'] else None
        r['sample']='Small sample' if r['decided_games']<20 else str(r['decided_games'])+' decided games'
        r['platforms']=', '.join(sorted(r['platforms']))
        r['last_report']=datetime.fromtimestamp(r.pop('last_report_epoch'),timezone.utc).isoformat()
        rows.append(r)
    rows.sort(key=lambda r:(-r['games'],r['user'], *(r.get(name, '') for name in dimensions)))
    return rows


def load_outcomes(path, key_rows, frm, to, owner_email='', owner_name='Owner'):
    bundles,subscribers,trials=read_snapshot(path)
    identities={}
    def remember(digest,label,kind,email=''):
        if not digest:return
        email=(email or '').strip().lower()
        if email and email==owner_email.lower():label,kind=owner_name,'owner'
        identities[digest[:16]]={'id':'email:'+email if email else 'uploader:'+digest[:16], 'user':label,'kind':kind}
    for token,alias,name,email in key_rows:
        if not alias:continue
        kind='trial' if alias.startswith('trial-') else 'subscriber' if alias.startswith('patreon-') else 'desktop key'
        remember(token,name or alias,kind,email)
    for r in subscribers:
        key=r.get('license_key')
        if key:remember(hashlib.sha256(key.encode()).hexdigest(),r.get('name') or r.get('email') or 'Subscriber','subscriber',r.get('email'))
    for r in trials:
        key=r.get('litellm_key')
        if key:
            digest=hashlib.sha256(key.encode()).hexdigest();old=identities.get(digest[:16],{})
            remember(digest,r.get('email') or old.get('user') or 'Trial '+digest[:8],'trial',r.get('email'))
    rows=aggregate(bundles,identities,frm,to)
    total={k:sum(r[k] for r in rows) for k in ['wins','losses','draws','unknown','games','decided_games','conflicts']}
    total['reporting_users']=len(rows)
    total['win_rate_pct']=round(100*total['wins']/total['decided_games'],1) if total['decided_games'] else None
    return {'users':rows,'totals':[total],
        'modes':aggregate(bundles,identities,frm,to,('play_mode','format_name')),
        'decks':aggregate(bundles,identities,frm,to,('play_mode','format_name','deck_name')), 'coverage':{'source':'Uploaded Coach outcome reports','time_basis':'First completed report received by server (UTC)',
        'win_rate':'wins / (wins + losses); draws, unknown and conflicting reports excluded',
        'scope':'Reported games only, not complete client history. Mode/deck views use uploaded metadata; missing or conflicting metadata is unknown. Legacy users view remains mixed.',
        'stored_reports':len(bundles),'stored_uploaders':len({b['owner'] for b in bundles})}}


def coach_license_hashes(path):
    _, subscribers, trials = read_snapshot(path)
    return sorted({hashlib.sha256(key.encode()).hexdigest() for row in subscribers + trials for key in [row.get("license_key") or row.get("litellm_key")] if key})
