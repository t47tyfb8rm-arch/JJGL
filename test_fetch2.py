import urllib.request
import re

url = 'https://fundf10.eastmoney.com/jjjz_011609.html'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
resp = urllib.request.urlopen(req, timeout=10)
html = resp.read().decode('utf-8')

# 查看所有 script 标签内容
script_matches = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
print(f'找到 {len(script_matches)} 个 script\n')
for i, s in enumerate(script_matches):
    if s.strip() and len(s) > 10:
        print(f'=== script[{i}] (len={len(s)}) ===')
        print(s[:300])
        print('...\n')

# 查找包含数据的 script （如 jsonpgz, Data_netWorthTrend）
for kw in ['Data_netWorthTrend', 'jsonpgz', 'fS_dailyGrowthRate', 'currentNetWorth', 'jz']:
    pos = html.find(kw)
    if pos >= 0:
        print(f'\n*** 找到 {kw} 位置 {pos} ***')
        print(html[max(0,pos-100):pos+300])
        print('...')

# 查找 table 标签
print('\n--- 查找 table 标签 ---')
tables = re.findall(r'<table[^>]*>[\s\S]*?</table>', html)
print(f'找到 {len(tables)} 个 table')
for i, t in enumerate(tables):
    print(f'\ntable[{i}] (len={len(t)}):')
    print(t[:500])
    print('...')

# 查找包含日期格式的内容 (如 2024-06-18)
print('\n--- 查找日期格式数据 ---')
date_matches = re.findall(r'(20\d{2}[-/]\d{1,2}[-/]\d{1,2}[^<"\']{0,200})', html)
print(f'找到 {len(date_matches)} 个日期相关数据')
for d in date_matches[:10]:
    print(f'  {d[:150]}')
