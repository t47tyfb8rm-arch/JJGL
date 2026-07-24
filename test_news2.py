import httpx
import re
import json

# 测试各种可能的基金资讯JSON接口
print('=== 测试基金资讯JSON接口 ===\n')

for code in ['011609', '004746']:
    print(f'\n--- 基金 {code} ---\n')

    # 尝试各种JSON接口
    json_urls = [
        (f'https://fundf10.eastmoney.com/js/fundgz_{code}.js', '估值'),
        (f'https://fund.eastmoney.com/js/fundarchive/js/data_{code}.js', '档案'),
        (f'https://push2.eastmoney.com/api/qt/content/getNewsListPlain?secid=0.{code}&fields=f58,f59,f60,f61,f62,f63,f64,f65&pageIndex=0&pageSize=5&sort=0&type=2', '新闻列表'),
    ]

    for url, desc in json_urls:
        try:
            resp = httpx.get(url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://fund.eastmoney.com/',
                'Accept': '*/*',
            })
            content = resp.text[:500]
            print(f'[{desc}] {resp.status_code} - {content[:200]}')
        except Exception as e:
            print(f'[{desc}] 错误: {e}')

    # 测试东方财富通用资讯搜索
    search_urls = [
        (f'https://search-api-web.eastmoney.com/search/jsonp?cb=&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22%E6%98%93%E6%96%B9%E8%BE%BE%E4%B8%8A%E8%AF%81%E7%A7%91%E5%88%9B50%22%2C%22type%22%3A%5B%22article%22%2C%22fund%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22param%22%3A%7B%7D%7D', '搜索'),
    ]

    for url, desc in search_urls:
        try:
            resp = httpx.get(url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0',
            })
            print(f'\n[{desc}] {resp.status_code}')
            print(f'内容: {resp.text[:500]}')
        except Exception as e:
            print(f'[{desc}] 错误: {e}')

    # 测试天天基金网的基金公告JSON
    gonggao_urls = [
        f'https://fundf10.eastmoney.com/FundData_js.aspx?type=jjgg&code={code}&pageIndex=1&pageSize=10&sdate=&edate=',
        f'https://fundf10.eastmoney.com/FundData_js.aspx?type=lsjz&code={code}&pageIndex=1&pageSize=10',
    ]

    for url in gonggao_urls:
        try:
            resp = httpx.get(url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://fundf10.eastmoney.com/',
            })
            print(f'\n[公告/净值JSON] {resp.status_code}')
            print(f'内容: {resp.text[:300]}')
        except Exception as e:
            print(f'错误: {e}')

    # 基金档案页面的行情信息
    try:
        resp = httpx.get(f'https://fund.eastmoney.com/{code}.html', timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0',
        })
        # 查找JSON格式数据
        json_matches = re.findall(r'var\s+(\w+)\s*=\s*(\{[^;]+\}|\[[^\]]+\])', resp.text)
        print(f'\n找到 {len(json_matches)} 个JS变量')
        for name, value in json_matches[:10]:
            if len(value) > 20:
                print(f'  var {name} = {value[:200]}')
    except Exception as e:
        print(f'页面错误: {e}')
