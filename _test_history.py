# 测试历史净值数据
import asyncio
import httpx
import re
import json
from datetime import datetime

# 天天基金网历史净值接口（JSONP格式）
# 方式1: HTML接口
# 方式2: JSON接口 - http://fund.eastmoney.com/js/fundjson.jsp? fund代码.js - 有

async def test_historical(fund_code):
    # 接口1: 历史净值JSON API
    url = f"http://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={fund_code}&page=1&per=10&sdate=&edate=&rt=json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"http://fund.eastmoney.com/{fund_code}.html",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            print(f"[{fund_code}] status={r.status_code}")
            print(f"Content: {r.text[:500]}")
            print("---")
    except Exception as e:
        print(f"[{fund_code}] ERR {e}")

async def main():
    for code in ["011609", "020741", "004746"]:
        await test_historical(code)
        print("="*60)

asyncio.run(main())
