import httpx
import json
import re

# 测试上证指数的各种接口
print('=== 测试上证指数 ===')

sh_tests = [
    # 新浪财经
    ('sina', 'https://hq.sinajs.cn/list=sh000001'),
    # 东方财富
    ('eastmoney_push2', 'https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f169,f170'),
    # 腾讯财经
    ('tencent', 'https://qt.gtimg.cn/q=sh000001'),
    # 同花顺
    ('ths', 'https://www.10jqka.com.cn/'),
]

for name, url in sh_tests:
    print(f'\n--- {name}: {url[:70]} ---')
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': f'https://{url.split("/")[2]}/',
            'Accept': '*/*',
        })
        print(f'状态: {resp.status_code}, 长度: {len(resp.content)}')
        text = resp.text
        print(f'内容: {text[:300]}')
    except Exception as e:
        print(f'错误: {e}')

# 测试新浪财经
print('\n=== 新浪财经解析 ===')
try:
    resp = httpx.get('https://hq.sinajs.cn/list=sh000001', timeout=10.0, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://finance.sina.com.cn/',
    })
    # GBK编码
    content = resp.content.decode('gbk')
    print(f'原始内容: {content[:500]}')
    # 解析
    match = re.search(r'="([^"]+)";', content)
    if match:
        data = match.group(1).split(',')
        print(f'解析后:')
        print(f'  名称: {data[0] if len(data) > 0 else "?"}')
        print(f'  开盘: {data[1] if len(data) > 1 else "?"}')
        print(f'  昨收: {data[2] if len(data) > 2 else "?"}')
        print(f'  当前: {data[3] if len(data) > 3 else "?"}')
        print(f'  最高: {data[4] if len(data) > 4 else "?"}')
        print(f'  最低: {data[5] if len(data) > 5 else "?"}')
        if len(data) > 3 and float(data[2]) > 0:
            change = (float(data[3]) - float(data[2])) / float(data[2]) * 100
            print(f'  涨跌: {change:.2f}%')
except Exception as e:
    print(f'错误: {e}')
