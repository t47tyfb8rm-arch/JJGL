import urllib.request
import re
import json

# 测试估算净值接口
for code in ['011609', '020741', '004746']:
    gz_url = f'https://fundgz.1234567.com/js/{code}.js?rt={__import__("time").time()}'
    req = urllib.request.Request(gz_url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://fund.eastmoney.com/',
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('utf-8')
        # 解析 JSONP
        if 'jsonpgz(' in data:
            json_data = data[len('jsonpgz('):-1]
            info = json.loads(json_data)
            print(f'\n基金 {code}:')
            print(f'  名称: {info["name"]}')
            print(f'  净值日期: {info["jzrq"]}')
            print(f'  单位净值: {info["dwjz"]}')
            print(f'  估算净值: {info["gsz"]}')
            print(f'  估算涨跌: {info["gszzl"]}%')
            print(f'  估算时间: {info["gztime"]}')
        else:
            print(f'\n基金 {code}: 无估算数据 - {data[:100]}')
    except Exception as e:
        print(f'\n基金 {code}: 错误 {e}')

# 测试历史净值接口
print('\n\n=== 测试历史净值接口 ===')
history_urls = [
    'https://fundf10.eastmoney.com/json/FundData_js.aspx?type=lsjz&code=011609&pageIndex=1&pageSize=5&sdate=&edate=',
    'https://fundf10.eastmoney.com/FundData_js.aspx?type=lsjz&code=011609&pageIndex=1&pageSize=5',
]

for url in history_urls:
    print(f'\n测试: {url}')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://fundf10.eastmoney.com/jjjz_011609.html',
        'X-Requested-With': 'XMLHttpRequest',
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('utf-8')
        print(f'长度: {len(data)}')
        print(f'内容: {data[:300]}')
    except Exception as e:
        print(f'错误: {e}')

# 测试 fund.eastmoney.com 的数据接口
print('\n\n=== 测试 fund.eastmoney.com 数据接口 ===')
for url2 in [
    'https://fund.eastmoney.com/js/fundarchive/js/data_011609.js',
    'https://fund.eastmoney.com/js/fundarchive/js/jjjz_011609.js',
]:
    print(f'\n测试: {url2}')
    req = urllib.request.Request(url2, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://fund.eastmoney.com/'})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('utf-8')
        print(f'长度: {len(data)}, 内容: {data[:300]}')
    except Exception as e:
        print(f'错误: {e}')
