import urllib.request

url = 'https://fundf10.eastmoney.com/jjjz_011609.html'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
resp = urllib.request.urlopen(req, timeout=10)
html = resp.read().decode('utf-8')

import re
keywords = ['净值', 'dwjz', 'jzrq', 'Data_netWorthTrend', 'Data_ACWorthTrend', 'Data_Fund', '011609']
for kw in keywords:
    matches = [(m.start(), html[max(0,m.start()-80):m.start()+150]) for m in re.finditer(kw, html)]
    if matches:
        print(f'\n--- 找到 "{kw}" 共{len(matches)}次 ---')
        for pos, context in matches[:3]:
            print(f'  位置 {pos}: ...{context}...')

# 查找JSON格式数据
print('\n--- 查找 script 标签 ---')
script_matches = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
print(f'找到 {len(script_matches)} 个 script')
for i, s in enumerate(script_matches[:5]):
    print(f'\nscript[{i}]: 长度 {len(s)}')
    print(s[:200])
