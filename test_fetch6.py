import httpx
import json

# 测试正确的域名
for code in ['011609', '020741', '004746']:
    for domain in ['fundgz.1234567.com.cn', 'fundgz.1234567.com']:
        gz_url = f'https://{domain}/js/{code}.js'
        print(f'\n测试 {gz_url}')
        try:
            resp = httpx.get(gz_url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://fund.eastmoney.com/',
                'Accept': '*/*',
            })
            data = resp.text
            if 'jsonpgz(' in data:
                json_data = data[len('jsonpgz('):-1]
                info = json.loads(json_data)
                print(f'  成功: {info["name"]}, 净值: {info["dwjz"]} ({info["jzrq"]}), 涨跌: {info["gszzl"]}%')
                break
            else:
                print(f'  返回: {data[:200]}')
        except Exception as e:
            print(f'  错误: {e}')

# 测试 push2 接口
print('\n=== 测试 push2.eastmoney.com ===')
for params_format in [
    'https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f44,f60,f170',
]:
    try:
        resp = httpx.get(params_format, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'http://quote.eastmoney.com/',
            'Accept': 'application/json, text/plain, */*',
        })
        print(f'状态: {resp.status_code}')
        print(f'内容: {resp.text[:500]}')
    except Exception as e:
        print(f'错误: {e}')

# 测试 fund.eastmoney.com 的数据接口
print('\n=== 测试 fund.eastmoney.com ===')
for code in ['011609', '020741', '004746']:
    for url2 in [
        f'https://fund.eastmoney.com/{code}.html',
        f'https://fund.eastmoney.com/js/fundarchive/js/data_{code}.js',
    ]:
        print(f'测试 {url2[:60]}')
        try:
            resp = httpx.get(url2, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://fund.eastmoney.com/',
            })
            # 从HTML中查找净值数据
            text = resp.text
            # 搜索 dwjz 或净值
            import re
            for kw in ['dwjz', 'gsz', '净值', '单位净值']:
                pos = text.find(kw)
                if pos >= 0:
                    print(f'  找到 "{kw}" 位置 {pos}: {text[max(0,pos-50):pos+100]}')
                    break
        except Exception as e:
            print(f'  错误: {e}')
