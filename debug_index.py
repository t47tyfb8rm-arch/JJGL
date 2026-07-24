# 快速测试：当前返回的上证指数实时值和历史数据
import httpx
import re
import json

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 1) 测试新浪实时数据
url = "https://hq.sinajs.cn/list=sh000001"
r = httpx.get(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn/"}, timeout=10)
content = r.content.decode('gbk', errors='ignore')
print("=== SINA 原始数据 ===")
print(content)
match = re.search(r'="([^"]+)";', content)
if match:
    parts = match.group(1).split(',')
    print(f"字段数量: {len(parts)}")
    for i, p in enumerate(parts):
        print(f"  [{i}] {p}")

# 2) 测试腾讯实时数据
print("\n=== 腾讯 原始数据 ===")
url2 = "https://qt.gtimg.cn/q=sh000001"
r2 = httpx.get(url2, headers={**HEADERS, "Referer": "https://qt.gtimg.cn/"}, timeout=10)
content2 = r2.content.decode('gbk', errors='ignore')
print(content2)
match2 = re.search(r'="([^"]+)";', content2)
if match2:
    parts2 = match2.group(1).split('~')
    print(f"字段数量: {len(parts2)}")
    for i, p in enumerate(parts2):
        print(f"  [{i}] {p[:60]}")

# 3) 测试历史K线
print("\n=== 腾讯K线 ===")
kurl = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param=sh000001,day,,,15,qfq&r=0.3"
kr = httpx.get(kurl, headers=HEADERS, timeout=10)
idx = kr.text.find("kline_dayqfq=")
if idx >= 0:
    json_str = kr.text[idx + 14:].rstrip(';').strip()
    kline = json.loads(json_str)
    days = kline['data']['sh000001']['day']
    print(f"总条数: {len(days)}")
    print(f"索引0: {days[0]}")
    print(f"索引-1(最后): {days[-1]}")
    # 打印最近10天
    for i, d in enumerate(days[-10:]):
        close = float(d[1])
        prev = float(d[2])
        chg = round((close - prev) / prev * 100, 2)
        print(f"  day[{len(days)-10+i:02d}]: {d[0]} 收={close} 昨={prev} 涨跌={chg}%")
