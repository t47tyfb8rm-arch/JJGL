import httpx

# 添加自定义休市日
r = httpx.post('http://localhost:8000/api/trading-calendar/holiday',
               json={"date": "2026-07-13", "action": "add"}, timeout=5)
print('添加 2026-07-13:', r.json())

# 查询确认
r = httpx.get('http://localhost:8000/api/trading-calendar?start=2026-07-10&end=2026-07-13', timeout=5)
d = r.json()
for day in d['days']:
    mark = '✓' if day['is_trading_day'] else '✗'
    wd = ['一','二','三','四','五','六','日'][day['weekday']]
    print(f"  {mark} {day['date']} 周{wd}")

# 删除回去
r = httpx.post('http://localhost:8000/api/trading-calendar/holiday',
               json={"date": "2026-07-13", "action": "remove"}, timeout=5)
print('\n删除 2026-07-13:', r.json())
