# 测试指数历史JSON解析
import httpx
import re
import json

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param=sh000001,day,,,10,qfq&r=0.1"
r = httpx.get(url, headers=HEADERS, timeout=10)
text = r.text
print(f"原始响应前200: {text[:200]}")
print(f"原始响应后100: {text[-100:]}")

# 方法1: 直接用text[位置:]
prefix = "kline_dayqfq="
idx = text.find(prefix)
if idx >= 0:
    json_str = text[idx + len(prefix):]
    # 去掉末尾的分号
    json_str = json_str.rstrip(';').strip()
    print(f"\n提取的JSON前100: {json_str[:100]}")
    print(f"提取的JSON后50: {json_str[-50:]}")
    data = json.loads(json_str)
    days = data['data']['sh000001']['day']
    print(f"\n成功! 共{len(days)}天数据:")
    for d in days[:7]:
        close = float(d[1])
        prev = float(d[2])
        chg = round((close - prev) / prev * 100, 2)
        sign = '+' if chg >= 0 else ''
        print(f"  {d[0]} 收盘{close:.2f} 涨跌{sign}{chg}%")
