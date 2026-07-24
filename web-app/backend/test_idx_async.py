import asyncio, httpx, re

async def fetch_realtime_index(code: str):
    try:
        url = f"https://qt.gtimg.cn/q={code}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text.strip()
        print(f"  text: {text[:200]}")
        m = re.search(r'="([^"]+)"', text)
        if not m:
            print("  no match")
            return {}
        parts = m.group(1).split('~')
        print(f"  parts count: {len(parts)}")
        if len(parts) < 33:
            print(f"  parts too short: {parts[:5]}")
            return {}
        result = {
            "code": code,
            "name": parts[1],
            "current": float(parts[3]),
            "previous": float(parts[4]),
            "change_amt": float(parts[31]),
            "change_pct": float(parts[32]),
        }
        print(f"  OK: {result}")
        return result
    except Exception as e:
        print(f"  ERR: {e}")
        return {}

async def main():
    for c in ["sh000688", "sh000016", "sh000300", "sh000905"]:
        print(f"--- {c}")
        await fetch_realtime_index(c)

asyncio.run(main())
