import httpx
import json
import re

# 测试多个资讯来源
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
}

# 测试东方财富基金资讯
print('=== 测试 fund.eastmoney.com 资讯 ===\n')

# 直接访问基金档案页 - 这个已经能有新闻了
for code in ['011609', '004746']:
    urls_to_test = [
        f'http://fund.eastmoney.com/a/{code}.html',
        f'http://fundf10.eastmoney.com/jjjz_{code}.html',
    ]
    for url in urls_to_test:
        try:
            resp = httpx.get(url, timeout=10.0, headers=headers, follow_redirects=True)
            text = resp.text
            print(f'[{code}] {resp.status_code} ({resp.url})')

            # 找标题包含基金相关的链接
            all_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]+)"[^>]*>([^<]*)</a>|<a[^>]*href="([^"]+)"[^>]*>([^<]{8,80})</a>', text)
            print(f'  链接数: {len(all_links)}')

            news_list = []
            for m in all_links:
                if m[1]:  # title存在
                    link, title = m[0], m[1]
                else:
                    link, title = m[3], m[4]
                title = re.sub(r'&nbsp;', '', title).strip()
                # 过滤广告和非资讯内容
                if any(x in title for x in ['下载', '手机', '申购', '费率', '开户', '登录', '首页', '基金公司', '活期', '私募', '保险', '理财']):
                    continue
                if any(x in link for x in ['fundgz', 'fund.eastmoney.com/news', 'fund.eastmoney.com/a', 'guba', 'eastmoney.com/news', 'kuaixun']):
                    if 10 < len(title) < 80:
                        news_list.append((title, link))

            print(f'  资讯数: {len(news_list)}')
            for title, link in news_list[:8]:
                print(f'    * {title[:60]} -> {link[:80]}')
            print()
        except Exception as e:
            print(f'  错误: {e}\n')

# 测试腾讯证券 - 简单新闻
print('\n=== 测试腾讯财经新闻 ===\n')

tencent_urls = [
    ('股票要闻', 'https://finance.qq.com/'),
    ('基金新闻', 'https://finance.qq.com/l/fund/fundnews/'),
]

for desc, url in tencent_urls:
    try:
        resp = httpx.get(url, timeout=10.0, headers=headers)
        text = resp.text
        print(f'[{desc}] {resp.status_code}')
        # 找新闻标题
        titles = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{8,80})</a>', text)
        count = 0
        for link, title in titles:
            title = title.strip()
            if len(title) > 8 and not any(x in title for x in ['Copyright', '腾讯', '友情']):
                if count < 8:
                    print(f'  * {title[:80]}')
                count += 1
        print(f'  共 {count} 条')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 测试新浪财经
print('=== 测试新浪财经 ===\n')

sina_urls = [
    ('新浪基金', 'https://finance.sina.com.cn/fund/'),
    ('新浪股票', 'https://finance.sina.com.cn/stock/'),
]

for desc, url in sina_urls:
    try:
        resp = httpx.get(url, timeout=10.0, headers=headers)
        text = resp.text
        print(f'[{desc}] {resp.status_code}')
        titles = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{8,80})</a>', text)
        count = 0
        for link, title in titles:
            title = title.strip()
            if len(title) > 8 and not any(x in title for x in ['新浪', 'Sina', 'Copyright', '友情']):
                if count < 8:
                    print(f'  * {title[:80]}')
                count += 1
        print(f'  共 {count} 条')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')

# 用东方财富搜索API - 正确参数
print('=== 东方财富搜索（修正） ===\n')

# 简单搜索 - 直接访问基金新闻首页
simple_urls = [
    ('基金新闻', 'https://fund.eastmoney.com/news/'),
]
for desc, url in simple_urls:
    try:
        resp = httpx.get(url, timeout=10.0, headers=headers, follow_redirects=True)
        text = resp.text
        print(f'[{desc}] {resp.status_code} ({resp.url})')
        titles = re.findall(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]{8,80})"', text)
        print(f'  带标题的链接: {len(titles)}')
        for link, title in titles[:10]:
            title = re.sub(r'&nbsp;', '', title).strip()
            if len(title) > 8:
                print(f'  * {title[:80]}')
        print()
    except Exception as e:
        print(f'[{desc}] 错误: {e}\n')
