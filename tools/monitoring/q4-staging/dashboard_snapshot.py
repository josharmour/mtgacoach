"""Put people and service health first; retain detailed operations in collapsed rows."""
from copy import deepcopy


def arrange_snapshot(panels):
    by_id = {p['id']: deepcopy(p) for p in panels}
    out = []

    def put(pid, title, x, y, w, h):
        p = by_id[pid]
        p['title'] = title
        p['gridPos'] = dict(x=x, y=y, w=w, h=h)
        out.append(p)
        return p

    def fields(p, names, labels):
        p['transformations'] = [
            {'id': 'filterFieldsByName', 'options': {'include': {'names': names}}},
            {'id': 'organize', 'options': {'indexByName': {n:i for i,n in enumerate(names)},
                                          'renameByName': labels}},
        ]
        p['options']['cellHeight'] = 'sm'
        # Match renamed fields and avoid inherited oversized preview columns.
        p['fieldConfig']['overrides'] = []
        for n in names:
            props = []
            if n in ('cost_usd',): props = [{'id':'unit','value':'currencyUSD'}, {'id':'decimals','value':4}]
            elif n in ('duration_s','avg_latency_s'): props = [{'id':'unit','value':'s'}, {'id':'decimals','value':2}]
            elif n.endswith('_tokens'): props = [{'id':'unit','value':'short'}]
            elif n in ('time','last_seen','expires'): props = [{'id':'custom.width','value':170}]
            if props: p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':labels.get(n,n)},'properties':props})
        for sort in p['options'].get('sortBy',[]):
            if sort['displayName'] in ('requests','Requests'): sort['displayName']='Requests'
            elif sort['displayName'] in ('cost_usd','Estimated cost'):sort['displayName']='API-equivalent cost'
            elif sort['displayName']=='time':sort['displayName']='Time'

    for pid,title,x in [(95116,'Sobriety Copilot · today',0), (87316,'Sobriety Copilot · 7 days',4),
                        (19462,'Sobriety Copilot · 30 days',8), (27388,'Coach trials using AI · range',12),
                        (79105,'Coach trials · valid keys now',16)]:
        p=put(pid,title,x,0,4,4)
        p['fieldConfig']['defaults']['decimals']=0
        if pid==27388:p['description']='Distinct trial keys with at least one recorded request in the selected dashboard range. Activity, not simultaneous online sessions.'
        if pid==79105:p['description']='Unexpired, unblocked trial keys right now; these users may not be currently using Coach.'
    sub=deepcopy(by_id[27388]);sub['id']=4701;sub['title']='Coach subscribers using AI · range'
    sub['gridPos']=dict(x=20,y=0,w=4,h=4)
    sub['targets'][0]['url']=sub['targets'][0]['url'].replace('/api/trials?','/api/subscribers?')
    sub['description']='Subscriber keys with at least one recorded request in the selected dashboard range.'
    out.append(sub)

    p=put(4500,'MTGA Coach · active trials in range',0,4,12,8)
    fields(p,['key_alias','email','requests','errors','last_seen'],
           {'key_alias':'Trial','email':'User','requests':'Requests','errors':'Errors','last_seen':'Last request'})
    p['description']='Trials that actually sent requests during the selected range, busiest first. Valid but unused trial keys are excluded.'
    p=put(4510,'MTGA Coach · active people in range',12,4,12,8)
    fields(p,['user','kind','requests','errors','cost_usd','last_seen'],
           {'user':'User','kind':'Access','requests':'Requests','errors':'Errors','cost_usd':'API-equivalent cost','last_seen':'Last request'})

    latency=deepcopy(by_id[4104]);latency['id']=4702
    latency['targets']=[deepcopy(by_id[4205]['targets'][-1])]
    latency['targets'][0].update(instant=True,range=False)
    latency['targets'][0]['expr']=latency['targets'][0]['expr'].replace('$__rate_interval','5m')
    latency['fieldConfig']['defaults'].update(unit='s',decimals=1)
    latency['description']='Backend end-to-end latency p95 over the last five minutes, across all request types. Not the tactical Coach deadline.'
    by_id[4702]=latency
    for pid,title,x in [(4702,'Latency p95 · last 5m',0),(4102,'Running · now',4),(4103,'Queued · now',8),
                        (49439,'Requests · selected range',12),(48617,'Errors · selected range',16),(13981,'API-equivalent cost · range',20)]:
        p=put(pid,title,x,12,4,4)
        if pid not in (13981,4702):p['fieldConfig']['defaults']['decimals']=0
    by_id[48617]['fieldConfig']['defaults']['thresholds']['steps']=[{'color':'green','value':None},{'color':'orange','value':1}]
    out.append({'id':4700,'title':'Public API cost basis','type':'text','gridPos':dict(x=0,y=16,w=24,h=3),
                'options':{'mode':'markdown','content':
                 '**Current backend: GLM-5.3-Flash** · Per 1M tokens: **$0.15 input · $0.03 cached input · $0.50 output**, including reasoning. '
                 '[Z.ai public pricing](https://docs.z.ai/guides/overview/pricing) · Checked Nov 3, 2026.\n\n'
                 'Estimates, not customer charges. Missing cache counts use full input price. Historical requests retain their own backend pricing. '
                 'User tiles show their named time window; charts and usage tables follow the dashboard range.'}})
    p=put(3001,'Usage by client · selected range',0,19,24,8)
    fields(p,['key_alias','requests','errors','prompt_tokens','completion_tokens','cost_usd','avg_latency_s','last_seen'],
           {'key_alias':'Client / key','requests':'Requests','errors':'Errors','prompt_tokens':'Input tokens','completion_tokens':'Output tokens',
            'cost_usd':'API-equivalent cost','avg_latency_s':'Avg latency','last_seen':'Last request'})
    put(87290,'Request history by client',0,27,12,8)
    put(88377,'Cost history by client',12,27,12,8)
    p=put(4520,'Sobriety Copilot · daily activity, last 30 days',0,35,12,8)
    p['timeFrom']='30d';p['hideTimeOverride']=False
    p['targets'][0]['url']=p['targets'][0]['url'].replace('days=366','days=30')
    p['description']='Distinct people per Los Angeles calendar day, with a fixed last-30-days window for context. Not a count of simultaneous online sessions.'
    put(4205,'Inference latency · first token and completion',12,35,12,8)
    p=put(3004,'Recent requests · client, result and cost',0,43,24,9)
    fields(p,['time','key_alias','status','duration_s','prompt_tokens','completion_tokens','cost_usd'],
           {'time':'Time','key_alias':'Client / key','status':'Result','duration_s':'Duration','prompt_tokens':'Input tokens',
            'completion_tokens':'Output tokens','cost_usd':'API-equivalent cost'})
    p['description']='Recent recorded gateway requests. Prompts, IP addresses, constant model aliases and empty trace links are omitted. Use Langfuse for payload investigation.'

    def collapsed(pid,title,y,ids):
        children=[];cy=y+1
        for i,k in enumerate(ids):
            p=by_id[k];w=24 if p['type']=='table' else 12;h=6 if p['type']=='stat' else 7
            p['gridPos']=dict(x=0,y=cy,w=w,h=h);cy+=h;children.append(p)
        out.append({'id':pid,'title':title,'type':'row','collapsed':True,'gridPos':dict(x=0,y=y,w=24,h=1),'panels':children})
    collapsed(4710,'Infrastructure details · throughput, GPU and cache',52,[4100,4104,4204,4206,4207,4200,4201,4202,4203])
    collapsed(4711,'Trial lifecycle, protection and history coverage',53,[85664,88697,9708,4400,4401,4600])
    # Report dimensions are explicit; old/missing/conflicting values remain unknown.
    for p in out:
        if p['gridPos']['y'] >= 12:
            p['gridPos']['y'] += 32
            for child in p.get('panels', []):
                child['gridPos']['y'] += 32
    for offset, (view, title) in enumerate((
        ('autopilot', 'autopilot'), ('manual', 'played by hand'),
        ('unknown', 'unknown play mode'), ('decks', 'per-deck results'),
    )):
        win = deepcopy(by_id[4510])
        win['id'] = 4720 + offset
        win['title'] = 'MTGA Coach · reported game win rate · ' + title
        win['gridPos'] = dict(x=0, y=12 + offset * 8, w=24, h=8)
        win['targets'][0]['url'] = win['targets'][0]['url'].replace(
            '/api/mtgacoach-users?view=top', '/api/mtgacoach-outcomes?view=' + view)
        names = ['user', 'access', 'play_mode', 'format_name']
        if view == 'decks':
            names.append('deck_name')
        names += ['win_rate_pct', 'wins', 'losses', 'draws', 'unknown', 'games', 'sample', 'last_report']
        labels = {'user': 'User', 'access': 'Access', 'play_mode': 'Play mode',
                  'format_name': 'Format', 'deck_name': 'Deck', 'win_rate_pct': 'Win rate',
                  'wins': 'Wins', 'losses': 'Losses', 'draws': 'Draws', 'unknown': 'Unknown',
                  'games': 'Reported games', 'sample': 'Sample', 'last_report': 'Last report'}
        fields(win, names, labels)
        strings = {'user', 'access', 'play_mode', 'format_name', 'deck_name', 'sample'}
        win['targets'][0]['columns'] = [
            {'selector': n, 'text': n, 'type': 'timestamp' if n == 'last_report'
             else 'string' if n in strings else 'number'} for n in names]
        win['options']['sortBy'] = [{'desc': True, 'displayName': 'Reported games'}]
        win['fieldConfig']['overrides'].append({
            'matcher': {'id': 'byName', 'options': 'Win rate'}, 'properties': [
                {'id': 'unit', 'value': 'percent'}, {'id': 'decimals', 'value': 1},
                {'id': 'noValue', 'value': '—'}]})
        win['description'] = ('Uploaded outcomes received in the selected range, deduplicated by resolved user and match. '
            'Win rate = wins/(wins+losses); draws and unknown/conflicting outcomes excluded. '
            'Fewer than 20 decided games are a small sample. Mode, format and deck come from reported metadata; '
            'missing/conflicting values remain unknown, never presumed manual. Missing uploads are not losses. '
            'Deck is the first strategy line, not a verified deck-list identity. Not complete client history.')
        out.append(win)
    return sorted(out, key=lambda p: (p['gridPos']['y'], p['gridPos']['x']))
