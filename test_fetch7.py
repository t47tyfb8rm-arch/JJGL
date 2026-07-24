import httpx
import json
import re
from bs4 import BeautifulSoup

# 测试基金信息解析
for code in ['011609', '020741', '004746']:
    url = f'https://fund.eastmoney.com/{code}.html'
    print(f'\n=== 基金 {code} ===')
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'https://fund.eastmoney.com/',
        })
        html = resp.text

        # 提取基金名称
        name_match = re.search(r'<title>([^(（]+)[(（]', html)
        fund_name = name_match.group(1).strip() if name_match else code
        print(f'名称: {fund_name}')

        # 提取净值日期和单位净值
        # 格式: fix_date">06-18：</span><span class="fix_dwjz...">1.4507</span>...fix_zzl...">3.68%
        date_match = re.search(r'fix_date">\((\d{2}-\d{2})\)：', html)
        nav_date = date_match.group(1) if date_match else '未知'

        nav_match = re.search(r'fix_dwjz[^>]*>([\d.]+)<', html)
        current_nav = nav_match.group(1) if nav_match else '0'

        zzl_match = re.search(r'fix_zzl[^>]*>([\d.-]+)%<', html)
        daily_change = zzl_match.group(1) if zzl_match else '0'

        # 基金类型 - 从 HTML 查找
        type_match = re.search(r'类型：</span><[^>]*>([^<]+)<', html)
        fund_type = type_match.group(1).strip() if type_match else '未知'

        print(f'净值日期: 2026-{nav_date}')
        print(f'单位净值: {current_nav}')
        print(f'日涨跌: {daily_change}%')
        print(f'基金类型: {fund_type}')

        # 也测试 fundgz 接口
        gz_url = f'https://fundgz.1234567.com.cn/js/{code}.js'
        try:
            gz_resp = httpx.get(gz_url, timeout=10.0, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://fund.eastmoney.com/',
            })
            gz_data = gz_resp.text
            print(f'\n估算接口: {gz_data[:200]}')
            # 修复JSONP解析 - 可能有换行
            if 'jsonpgz(' in gz_data:
                json_str = gz_data[gz_data.find('jsonpgz(')+7:]
                json_str = json_str[:json_str.rfind(')')]
                gz_info = json.loads(json_str)
                print(f'估算净值: {gz_info["gsz"]} ({gz_info["gszzl"]}%)')
        except Exception as e:
            print(f'估算接口错误: {e}')

    except Exception as e:
        print(f'错误: {e}')

# 测试上证指数
print('\n\n=== 上证指数 ===')
# 尝试不同的接口
sh_urls = [
    'https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f60,f170',
    'https://push2his.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f60,f170',
    'https://quote.eastmoney.com/000001.html',
]

for url in sh_urls:
    print(f'\n测试 {url[:60]}')
    try:
        resp = httpx.get(url, timeout=10.0, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'https://quote.eastmoney.com/',
            'Accept': 'application/json, text/plain, */*',
        })
        print(f'状态: {resp.status_code}, 长度: {len(resp.text)}')
        if resp.status_code == 200 and resp.text.strip():
            # 尝试JSON
            try:
                data = resp.json()
                print(f'JSON: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}')
            except:
                # 从HTML提取
                text = resp.text
                # 查找价格和涨跌
                for kw in ['price', 'updown', 'zs_price', '上证指数', 'SH000001']:
                    pos = text.find(kw)
                    if pos >= 0:
                        print(f'  找到 "{kw}" 位置 {pos}: {text[max(0,pos-30):pos+80]}')
                        break
    except Exception as e:
        print(f'错误: {e}')
