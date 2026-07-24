import urllib.request
import re

# 测试天天基金网的JSON数据接口
test_urls = [
    'https://fundf10.eastmoney.com/json/Data_xxx_011609.js',
    'https://fundf10.eastmoney.com/json/FundData_js.aspx?type=lsjz&code=011609&pageIndex=1&pageSize=10',
    'https://fundf10.eastmoney.com/json/Data_011609.js',
    'https://fundf10.eastmoney.com/json/FundData_js.aspx?type=lsjz&code=011609&pageIndex=1&pageSize=10&sdate=&edate=',
]

for url in test_urls:
    print(f'\n=== 测试: {url} ===')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://fundf10.eastmoney.com/jjjz_011609.html',
        'Accept': '*/*',
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('utf-8')
        print(f'状态: {resp.status}, 长度: {len(data)}')
        print(f'内容: {data[:500]}')
    except Exception as e:
        print(f'错误: {e}')

# 也测试一下估算净值接口
print('\n=== 测试估算净值接口 ===')
gz_url = 'https://fundgz.1234567.com.cn/js/011609.js'
req = urllib.request.Request(gz_url, headers={
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://fund.eastmoney.com/',
})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode('utf-8')
    print(f'状态: {resp.status}')
    print(f'内容: {data[:500]}')
except Exception as e:
    print(f'错误: {e}')
