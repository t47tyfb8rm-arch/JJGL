# 测试基金专属资讯 - 抓取基金档案页中的新闻链接内容
import httpx
import re
import asyncio
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

FUND_NAMES = {
    "011609": "易方达上证科创50",
    "020741": "华泰保兴安悦债券",
    "004746": "易方达上证50",
}

async def fetch_fund_news(fund_code, fund_name):
    """获取某只基金的专属资讯"""
    all_news = []

    # 方式1: 抓取基金档案页中的新闻链接，再获取标题
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        page_url = f"https://fund.eastmoney.com/{fund_code}.html"
        r = await client.get(page_url, headers=HEADERS)
        if r.status_code != 200:
            return all_news

        # 找基金档案页中的新闻链接（/a/数字.html格式）
        article_links = re.findall(r'(fund\.eastmoney\.com/a/\d+\.html)', r.text)
        unique_links = list(dict.fromkeys(f"https://{l}" for l in article_links))[:8]

        # 获取每条新闻的标题
        for link in unique_links:
            try:
                nr = await client.get(link, headers=HEADERS, timeout=5.0)
                if nr.status_code == 200:
                    title_match = re.search(r'<title>([^<]+)</title>', nr.text)
                    if title_match:
                        title = title_match.group(1).strip()
                        # 过滤
                        if len(title) > 5 and '404' not in title:
                            all_news.append({
                                "title": title,
                                "url": link,
                                "time": ""
                            })
            except:
                pass
            await asyncio.sleep(0.1)

    return all_news[:5]


async def fetch_index_history(days=7):
    """获取上证指数历史数据"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param=sh000001,day,,," + str(days+2) + ",qfq&r=0.1"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        r = await client.get(url, headers=HEADERS)
        text = r.text
        # 解析JSON: kline_dayqfq={...};
        json_match = re.search(r'kline_dayqfq=({.*});\s*$', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(1))
            days_data = data['data']['sh000001']['day']
            result = []
            for d in days_data[:days]:
                close = float(d[1])
                prev_close = float(d[2])
                change = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                result.append({
                    "date": d[0],
                    "close": close,
                    "change": change,
                    "high": float(d[3]) if len(d) > 3 else 0,
                    "low": float(d[4]) if len(d) > 4 else 0,
                })
            return result
    return []


async def main():
    # 测试指数历史
    print("=== 上证指数7天历史 ===")
    history = await fetch_index_history(7)
    for h in history:
        sign = '+' if h['change'] >= 0 else ''
        print(f"  {h['date']} 收盘:{h['close']:.2f} 涨跌:{sign}{h['change']}%")

    # 测试基金专属资讯
    for code, name in FUND_NAMES.items():
        print(f"\n=== {code} {name} 专属资讯 ===")
        news = await fetch_fund_news(code, name)
        if news:
            for n in news:
                print(f"  - {n['title'][:50]}")
        else:
            print("  无专属资讯")
        await asyncio.sleep(0.5)

asyncio.run(main())
