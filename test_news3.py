import httpx
import json
import re
from urllib.parse import quote

# 测试搜索API获取基金资讯
funds = [
    ('011609', '易方达上证科创50联接C', '易方达上证科创50'),
    ('004746', '易方达上证50增强C', '易方达上证50增强'),
    ('020741', '华泰保兴安悦债券C', '华泰保兴安悦债券'),
]

for code, name, keyword in funds:
    print(f'\n=== 基金 {code} ({name}) ===')

    # 用基金名称搜索
    encoded_kw = quote(keyword)
    search_url = f'https://search-api-web.eastmoney.com/search/jsonp?cb=&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{encoded_kw}%22%2C%22type%22%3A%5B%22article%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22param%22%3A%7B%22pageSize%22%3A10%2C%22pageIndex%22%3A1%7D%7D'

    try:
        resp = httpx.get(search_url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Referer': 'https://www.eastmoney.com/',
        })
        print(f'状态: {resp.status_code}')

        # 解析 JSONP 格式: ({...})
        text = resp.text.strip()
        if text.startswith('(') and text.endswith(')'):
            text = text[1:-1]

        try:
            data = json.loads(text)
            articles = data.get('result', {}).get('article', [])
            print(f'找到 {len(articles)} 篇文章')

            for i, art in enumerate(articles[:5]):
                title = art.get('title', '')
                # 移除HTML标签
                title = re.sub(r'<[^>]+>', '', title)
                date = art.get('date', '')
                source = art.get('nickname', '')
                content = art.get('content', '')
                content = re.sub(r'<[^>]+>', '', content) if content else ''

                print(f'\n  [{i+1}] {title[:60]}')
                print(f'      来源: {source} | 时间: {date}')
                if content:
                    print(f'      摘要: {content[:80]}...')
        except Exception as e:
            print(f'JSON解析错误: {e}')
            print(f'原始内容: {text[:300]}')

    except Exception as e:
        print(f'错误: {e}')

# 测试上证指数资讯
print('\n=== 上证指数资讯 ===')
sh_url = 'https://search-api-web.eastmoney.com/search/jsonp?cb=&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22%E4%B8%8A%E8%AF%81%E6%8C%87%E6%95%B0%22%2C%22type%22%3A%5B%22article%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22param%22%3A%7B%22pageSize%22%3A8%2C%22pageIndex%22%3A1%7D%7D'

try:
    resp = httpx.get(sh_url, timeout=10.0, headers={
        'User-Agent': 'Mozilla/5.0',
        'Accept': '*/*',
    })
    text = resp.text.strip()
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1]
    data = json.loads(text)
    articles = data.get('result', {}).get('article', [])
    print(f'找到 {len(articles)} 篇文章')
    for i, art in enumerate(articles[:5]):
        title = re.sub(r'<[^>]+>', '', art.get('title', ''))
        date = art.get('date', '')
        source = art.get('nickname', '')
        print(f'  [{i+1}] {title[:60]} ({date} - {source})')
except Exception as e:
    print(f'错误: {e}')
