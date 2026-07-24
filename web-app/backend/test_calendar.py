import httpx

# 基本查询
r = httpx.get('http://localhost:8000/api/trading-calendar', timeout=5)
d = r.json()
print('今天 (2026-07-10) 是否是交易日:', d['is_today_trading_day'])
print('已配置休市日数量:', d['holidays_count'])
print('前 5 个休市日:', d['holidays'][:5])
print('最后 5 个休市日:', d['holidays'][-5:])

# 区间查询
print('\n--- 本周 (07-10 ~ 07-17) ---')
r = httpx.get('http://localhost:8000/api/trading-calendar?start=2026-07-10&end=2026-07-17', timeout=5)
d = r.json()
for day in d['days']:
    mark = '✓' if day['is_trading_day'] else '✗'
    wd = ['一','二','三','四','五','六','日'][day['weekday']]
    print(f"  {mark} {day['date']} 周{wd}")

# 国庆区间
print('\n--- 国庆 (10-01 ~ 10-08) ---')
r = httpx.get('http://localhost:8000/api/trading-calendar?start=2026-10-01&end=2026-10-08', timeout=5)
d = r.json()
for day in d['days']:
    mark = '✓' if day['is_trading_day'] else '✗'
    wd = ['一','二','三','四','五','六','日'][day['weekday']]
    print(f"  {mark} {day['date']} 周{wd}")
