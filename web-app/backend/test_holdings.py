import httpx
import re

url = 'https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=011609&topline=10'
headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://fundf10.eastmoney.com/ccmx_011609.html'
}
r = httpx.get(url, headers=headers, timeout=10)
text = r.text

# 提取content
m = re.search(r'content:"([^"]+)"', text)
if not m:
    print("No content found")
    exit()

content = m.group(1)
print("Content extracted, length:", len(content))

# 解析每一行<tr>...</tr>
rows = re.findall(r"<tr>(.*?)</tr>", content, re.DOTALL)
print(f"Found {len(rows)} rows")

holdings = []
for row in rows[:10]:
    # 股票代码: <td><a href='...'>688256</a></td>
    code_match = re.search(r"<a href='[^']*'>(\d+)</a>", row)
    # 股票名称: <td class='tol'><a href='...'>寒武纪</a></td>
    name_match = re.search(r"class='tol'><a[^>]*>([^<]+)</a>", row)
    # 占净值比例: <td class='tor'>0.03%</td> - 这是持仓比例，在第7列
    ratio_match = re.search(r"<td class='tor'>([\d.]+)%</td>", row)

    if code_match and name_match:
        code = code_match.group(1)
        name = name_match.group(1)
        ratio = float(ratio_match.group(1)) if ratio_match else 0.0
        holdings.append({"code": code, "name": name, "proportion": ratio, "net_ratio": ratio})
        print(f"  {code} {name} {ratio}%")

print(f"\nTotal holdings parsed: {len(holdings)}")