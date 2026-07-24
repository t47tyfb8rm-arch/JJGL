import urllib.request
import json
import socket

# 先测试DNS
for host in ['fundgz.1234567.com', 'fundgz.1234567.com.cn', 'fundf10.eastmoney.com', 'push2.eastmoney.com']:
    try:
        ip = socket.gethostbyname(host)
        print(f'{host} -> {ip}')
    except Exception as e:
        print(f'{host} -> 失败: {e}')

# 直接用 httpx 测试
print('\n=== 用 httpx 测试 ===')
import httpx

for code in ['011609', '020741', '004746']:
    gz_url = f'https://fundgz.1234567.com/js/{code}.js'
    print(f'\n测试 {gz_url}')
    try:
        resp = httpx.get(gz_url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://fund.eastmoney.com/',
        })
        data = resp.text
        if 'jsonpgz(' in data:
            json_data = data[len('jsonpgz('):-1]
            info = json.loads(json_data)
            print(f'  成功: {info["name"]}, 净值: {info["dwjz"]}, 涨跌: {info["gszzl"]}%')
        else:
            print(f'  返回: {data[:200]}')
    except Exception as e:
        print(f'  错误: {e}')

# 也测试 push2 接口获取上证指数
print('\n=== 测试上证指数 ===')
try:
    url = 'https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f170'
    resp = httpx.get(url, timeout=10.0, headers={'User-Agent': 'Mozilla/5.0'})
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
except Exception as e:
    print(f'错误: {e}')
