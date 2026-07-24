import httpx
import re
from datetime import datetime

# 测试天天基金网基金详情页的资讯部分
for code in ['011609', '004746']:
    print(f'\n=== 测试基金 {code} 资讯 ===')

    # 方法1: 基金详情页 - 基金公告
    urls_to_try = [
        (f'https://fundf10.eastmoney.com/jggg_{code}.html', '机构公告'),
        (f'https://fundf10.eastmoney.com/jjgg_{code}.html', '基金公告'),
        (f'https://fund.eastmoney.com/news/{code}.html', '基金新闻'),
        (f'https://fund.eastmoney.com/a/{code}.html', '基金档案'),
    ]

    for url, desc in urls_to_try:
        try:
            resp = httpx.get(url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Referer': 'https://fund.eastmoney.com/',
            })
            print(f'\n[{desc}] {url}')
            print(f'  状态: {resp.status_code}, 长度: {len(resp.text)}')

            if resp.status_code == 200 and len(resp.text) > 100:
                # 查找带日期的链接（资讯标题）
                # 尝试匹配: 2026-xx-xx 或 xx月xx日
                text = resp.text

                # 找包含日期的标题行
                patterns = [
                    r'(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)[^<]{0,50}',
                    r'(\d{2}-\d{2})[^<]{0,60}',
                ]

                # 查找table中的行
                rows = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', text)
                print(f'  找到 {len(rows)} 个table行')

                for row in rows[:10]:
                    # 提取文本
                    row_text = re.sub(r'<[^>]+>', ' ', row).strip()
                    row_text = re.sub(r'\s+', ' ', row_text)
                    if len(row_text) > 15 and len(row_text) < 200:
                        print(f'    > {row_text[:100]}')

                # 查找a标签中的新闻
                news_links = re.findall(r'<a[^>]*title="([^"]+)"[^>]*href="([^"]+)"', text)
                print(f'  找到 {len(news_links)} 个带title的链接')
                for title, link in news_links[:5]:
                    clean_title = title.strip()
                    if len(clean_title) > 5 and len(clean_title) < 100:
                        print(f'    * {clean_title[:80]} -> {link[:80]}')

                # 查找带日期格式的纯文本
                date_items = re.findall(r'(20\d{2}[-/\.]\d{1,2}[-/\.]\d{1,2})[^<]{1,80}', text)
                print(f'  找到 {len(date_items)} 个日期项')
                for item in date_items[:10]:
                    clean = re.sub(r'\s+', ' ', item).strip()
                    print(f'    - {clean[:100]}')

        except Exception as e:
            print(f'  错误: {e}')
