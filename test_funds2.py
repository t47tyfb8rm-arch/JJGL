import urllib.request, json
r = urllib.request.urlopen('http://localhost:8000/api/portfolio', timeout=60)
d = json.loads(r.read().decode('utf-8'))
funds = d.get('funds', [])
print('Total funds:', len(funds))
for i, f in enumerate(funds):
    code = f.get('code')
    name = f.get('name', '')
    est = f.get('estimatedChange', 0)
    nav = f.get('currentNav', 0)
    typ = f.get('type', '')
    navDate = f.get('navDate', '')
    dailyChg = f.get('dailyChange', 0)
    print('  %d. %s: %s [%s] est=%.3f%% daily=%.3f%% nav=%s date=%s' % (i+1, code, name[:30], typ, est, dailyChg, nav, navDate))
