import httpx
import json
import re

# 测试东方财富快讯JSON接口
print('=== 测试东方财富快讯接口 ===\n')

# 快讯API
kuaixun_urls = [
    ('东方财富-快讯-主页', 'https://kuaixun.eastmoney.com/'),
    ('东方财富-快讯-滚动', 'https://kuaixun.eastmoney.com/roll.html'),
]

for desc, url in kuaixun_urls:
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        })
        print(f'[{desc}] {resp.status_code}')
        text = resp.text

        # 找JSONP接口调用
        js_urls = re.findall(r'(https?://[^"\']+[^"\']json[^"\']*)', text)
        print(f'  JSON URLs: {len(js_urls)}')
        for u in js_urls[:5]:
            print(f'    {u[:100]}')

        # 找JSON数据
        json_data = re.findall(r'window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>', text)
        print(f'  __INITIAL_STATE__ 匹配: {len(json_data)}')

        # 查找 data-json 属性
        data_attrs = re.findall(r'data-json="([^"]+)"', text)
        print(f'  data-json 属性: {len(data_attrs)}')
        for d in data_attrs[:2]:
            print(f'    {d[:200]}')

        # 查找script中的JSON
        scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', text)
        print(f'  script 数量: {len(scripts)}')
        for i, s in enumerate(scripts[:5]):
            if len(s) > 50:
                print(f'    script[{i}]: {s[:200]}')
        print()

    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 直接测试一些已知的JSON接口
print('=== 测试JSON接口 ===\n')

json_endpoints = [
    ('东方财富-快讯-列表', 'https://kuaixun.eastmoney.com/api/getArticleList?type=0&pageIndex=1&pageSize=10&columnCode='),
    ('东方财富-快讯-财经', 'https://kuaixun.eastmoney.com/api/getArticleList?type=3&pageIndex=1&pageSize=10'),
    ('push2-新闻', 'https://push2.eastmoney.com/api/qt/content/getNewsListPlain?secid=1.000001&pageIndex=0&pageSize=10'),
    ('东方财富-财经新闻', 'https://www.eastmoney.com/'),
]

for desc, url in json_endpoints:
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'application/json,text/html,*/*',
        })
        print(f'[{desc}] {resp.status_code}')
        if resp.status_code == 200:
            text = resp.text
            # 尝试解析JSON
            try:
                data = json.loads(text)
                print(f'  JSON keys: {list(data.keys())[:10]}')
                # 查找文章列表
                for key in ['data', 'result', 'Data', 'list', 'items']:
                    if key in data and isinstance(data[key], list):
                        print(f'  {key}: {len(data[key])} 项')
                        for item in data[key][:3]:
                            if isinstance(item, dict):
                                print(f'    - {str(list(item.values())[:3])[:80]}')
            except:
                # 不是纯JSON，看看HTML里有没有
                if 'article' in text.lower() or 'news' in text.lower():
                    news = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{10,80})</a>', text)
                    print(f'  HTML news links: {len(news)}')
                    for link, title in news[:5]:
                        clean = title.strip()
                        if clean and len(clean) > 8:
                            print(f'    * {clean[:80]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 尝试 fund.eastmoney.com 的资讯页面
print('=== 测试 fund.eastmoney.com 资讯 ===\n')

fund_pages = [
    ('011609资讯', 'http://fund.eastmoney.com/news/011609.html'),
    ('011609详情', 'http://fund.eastmoney.com/011609.html'),
    ('011609公告', 'http://fundf10.eastmoney.com/jjgg_011609.html'),
]

for desc, url in fund_pages:
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'text/html,*/*',
        })
        print(f'[{desc}] {resp.status_code} - {resp.url}')
        if resp.status_code == 200:
            text = resp.text
            # 查找新闻
            news = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{5,80})</a>', text)
            filtered = [(l, t.strip()) for l, t in news if len(t.strip()) > 8 and not any(x in t.lower() for x in ['基金代码', '净值', '涨跌', '登录', '开户', '购买'])]
            print(f'  找到 {len(filtered)} 条新闻')
            for link, title in filtered[:10]:
                print(f'    * {title[:80]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')
