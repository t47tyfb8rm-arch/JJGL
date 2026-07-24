import httpx
import json
import re
from urllib.parse import urlencode

# 用更简单的方式 - 直接测试不同资讯来源

print('=== 测试不同资讯来源 ===\n')

# 方法1: 天天基金网新闻列表
fund_news_urls = [
    ('天天基金-要闻', 'https://fund.eastmoney.com/news/cywjh.html'),
    ('天天基金-公告', 'https://fund.eastmoney.com/news/gonggao.html'),
    ('东方财富-基金', 'http://fund.eastmoney.com/data/fundnews.html'),
    ('东方财富-资讯', 'https://kuaixun.eastmoney.com/'),
]

for desc, url in fund_news_urls:
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'text/html,*/*',
        })
        print(f'[{desc}] {resp.status_code} - {url}')
        if resp.status_code == 200:
            text = resp.text
            # 找新闻链接
            news_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]+)"[^>]*>', text)
            print(f'  找到 {len(news_links)} 个带标题的链接')
            for link, title in news_links[:5]:
                if len(title) > 5 and len(title) < 100:
                    print(f'    * {title[:60]} -> {link[:80]}')
            # 查找包含日期的标题
            dated_items = re.findall(r'(20\d{2}[-/\.]\d{1,2}[-/\.]\d{1,2})[^<]{3,120}', text)
            print(f'  找到 {len(dated_items)} 个带日期的项')
            for item in dated_items[:5]:
                clean = re.sub(r'\s+', ' ', item).strip()
                print(f'    - {clean[:80]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 测试新浪财经资讯
print('\n=== 新浪财经资讯 ===')
try:
    resp = httpx.get('https://finance.sina.com.cn/roll/index.d.html', timeout=10.0, headers={
        'User-Agent': 'Mozilla/5.0',
    })
    if resp.status_code == 200:
        # 查找新闻标题
        items = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{5,60})</a>', resp.text)
        print(f'找到 {len(items)} 条新闻')
        for link, title in items[:10]:
            clean_title = title.strip()
            if clean_title and len(clean_title) > 5:
                print(f'  * {clean_title[:80]}')
except Exception as e:
    print(f'错误: {e}')

# 测试腾讯证券
print('\n=== 腾讯证券资讯 ===')
try:
    resp = httpx.get('https://finance.qq.com/finance/stock/gp.htm', timeout=10.0, headers={
        'User-Agent': 'Mozilla/5.0',
    })
    if resp.status_code == 200:
        items = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{5,60})</a>', resp.text)
        print(f'找到 {len(items)} 条新闻')
        for link, title in items[:10]:
            clean_title = title.strip()
            if clean_title and len(clean_title) > 5:
                print(f'  * {clean_title[:80]}')
except Exception as e:
    print(f'错误: {e}')

# 测试天天基金网资讯JSON
print('\n=== 测试天天基金JSON ===')
for jsonp_url in [
    'http://fundgz.1234567.com.cn/news/js/fundnews_011609.js',
    'http://fund.eastmoney.com/js/fundarchive/js/fund_011609.js',
    'http://fund.eastmoney.com/data/FundNewsData.aspx?type=1&page=1&rows=10',
]:
    try:
        resp = httpx.get(jsonp_url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'http://fund.eastmoney.com/',
        })
        print(f'{jsonp_url.split("/")[-1][:50]}: {resp.status_code} -> {resp.text[:200]}')
    except Exception as e:
        print(f'错误: {e}')
