import urllib.request, json
r = urllib.request.urlopen('http://localhost:8000/api/portfolio', timeout=60)
d = json.loads(r.read().decode('utf-8'))
funds = d.get('funds', [])
print('funds count:', len(funds))
for f in funds:
    code = f.get('code')
    name = f.get('name', '')
    est = f.get('estimatedChange', 0)
    nav = f.get('currentNav', 0)
    typ = f.get('type', '')
    print('  %s: %s [%s] est=%.3f%% nav=%s' % (code, name[:30], typ, est, nav))

# 错误信息
errs = d.get('errors', [])
if errs:
    print('errors:', errs)
