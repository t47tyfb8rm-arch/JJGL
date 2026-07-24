import httpx
import json
import re
from urllib.parse import quote

print('=== 测试东方财富财经资讯 ===\n')

# 测试东方财富搜索API（带参数）
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# 东方财富搜索API - 用不同参数
search_tests = [
    ('基金要闻', 'https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery_call&param=' + quote('{"uid":"","keyword":"基金要闻","type":["article"],"client":"web","clientType":"web","param":{"pageSize":10,"pageIndex":1}}')),
    ('科创50', 'https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery_call&param=' + quote('{"uid":"","keyword":"科创50","type":["article"],"client":"web","clientType":"web","param":{"pageSize":10,"pageIndex":1}}')),
    ('债券基金', 'https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery_call&param=' + quote('{"uid":"","keyword":"债券基金","type":["article"],"client":"web","clientType":"web","param":{"pageSize":10,"pageIndex":1}}')),
]

for desc, url in search_tests:
    try:
        resp = httpx.get(url, timeout=10.0, headers=headers)
        text = resp.text
        print(f'[{desc}] {resp.status_code}, 长度: {len(text)}')
        # 解析JSONP
        if 'jQuery_call(' in text:
            json_text = text[text.find('(')+1:text.rfind(')')]
            try:
                data = json.loads(json_text)
                articles = data.get('result', {}).get('article', [])
                print(f'  找到 {len(articles)} 篇文章')
                for art in articles[:5]:
                    title = re.sub(r'<[^>]+>', '', art.get('title', ''))
                    date = art.get('date', '')
                    content = re.sub(r'<[^>]+>', '', art.get('content', '') or '')[:50]
                    print(f'    * [{date}] {title[:60]}')
            except Exception as e:
                print(f'  JSON解析错误: {e}')
                print(f'  内容: {text[:200]}')
        else:
            print(f'  不是JSONP: {text[:200]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 测试基金相关指数
print('=== 测试相关指数/指数资讯 ===\n')

# 用东方财富的行情资讯页
index_pages = [
    ('科创50指数', 'http://quote.eastmoney.com/kszt/000688.html'),
    ('上证50指数', 'http://quote.eastmoney.com/kszt/000016.html'),
    ('上证指数', 'http://quote.eastmoney.com/kszt/000001.html'),
]

for desc, url in index_pages:
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True, headers=headers)
        text = resp.text
        print(f'[{desc}] {resp.status_code}')
        # 找相关资讯
        news = re.findall(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]+)"[^>]*>', text)
        relevant = [(l, t) for l, t in news if len(t) > 10 and not any(x in t for x in ['东方财富', '下载', '开户'])]
        print(f'  找到 {len(relevant)} 条资讯')
        for link, title in relevant[:5]:
            print(f'    * {title[:60]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 测试用东方财富的"股吧"或"话题"页面
print('=== 测试东方财富股吧/话题 ===\n')
topic_urls = [
    ('科创50话题', 'http://guba.eastmoney.com/list,688256.html'),
]

for desc, url in topic_urls:
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True, headers=headers)
        text = resp.text
        print(f'[{desc}] {resp.status_code}')
        # 找标题
        titles = re.findall(r'<a[^>]*title="([^"]{5,80})"[^>]*href="([^"]+)"', text)
        print(f'  找到 {len(titles)} 条')
        for title, link in titles[:8]:
            clean = title.strip()
            if len(clean) > 8:
                print(f'    * {clean[:80]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')
