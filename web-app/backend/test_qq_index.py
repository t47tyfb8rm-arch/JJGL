import httpx, re
r = httpx.get('https://qt.gtimg.cn/q=sh000688', timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
text = r.text.strip()
print('原始:', text[:200])
m = re.search(r'="([^"]+)"', text)
if m:
    parts = m.group(1).split('~')
    print('parts 数量:', len(parts))
    print('name:', parts[1])
    print('current:', parts[3])
    print('previous:', parts[4])
    print('change_amt:', parts[31])
    print('change_pct:', parts[32])
