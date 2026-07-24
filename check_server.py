# -*- coding: utf-8 -*-
"""
云服务器端验证脚本 - 在 124.221.184.106 上运行
用于排查"只显示2只基金"的问题
"""
import urllib.request, json, sys

print("=" * 60)
print("基金管家 - 云服务器部署验证")
print("=" * 60)

# 1. 健康检查
try:
    r = urllib.request.urlopen('http://localhost:8000/api/health', timeout=10)
    print("[1] 健康检查: OK")
except Exception as e:
    print(f"[1] 健康检查: FAIL - {e}")
    sys.exit(1)

# 2. 拉取 portfolio 数据
try:
    r = urllib.request.urlopen('http://localhost:8000/api/portfolio', timeout=60)
    d = json.loads(r.read().decode('utf-8'))
    funds = d.get('funds', [])
    print(f"\n[2] /api/portfolio 返回基金数: {len(funds)}")
    for i, f in enumerate(funds):
        code = f.get('code')
        name = f.get('name', '')
        est = f.get('estimated_change', 0)
        typ = f.get('type', '')
        print(f"     {i+1}. {code} | {name[:30]} | [{typ}] est={est:.3f}%")
except Exception as e:
    print(f"[2] /api/portfolio: FAIL - {e}")
    sys.exit(1)

# 3. 静态检查 main.py 中 WATCHED_FUNDS
try:
    with open('/opt/fund-manager/web-app/backend/main.py', 'r', encoding='utf-8') as f:
        content = f.read()
    if 'WATCHED_FUNDS' in content:
        # 找到列表内容
        idx = content.find('WATCHED_FUNDS = [')
        if idx >= 0:
            end = content.find(']', idx)
            block = content[idx:end+1]
            print(f"\n[3] main.py 中 WATCHED_FUNDS 配置:")
            for line in block.split('\n'):
                line = line.strip()
                if line.startswith('"') and ',' in line:
                    print(f"     {line}")
            count = block.count('"') // 2
            if count != 3:
                print(f"\n     !! 警告: 配置中只有 {count} 只基金，应为 3 只 !!")
            else:
                print(f"\n     ✓ 配置正确（3 只基金）")
    else:
        print("[3] main.py 中找不到 WATCHED_FUNDS")
except FileNotFoundError:
    print("[3] /opt/fund-manager/web-app/backend/main.py 不存在")
    print("    请确认部署路径（可能在 /root/、/home/、/srv/ 等位置）")

print("\n" + "=" * 60)
print("如显示2只基金，请执行:")
print("  cd /opt/fund-manager/web-app/backend")
print("  grep -A 5 'WATCHED_FUNDS = \\[' main.py")
print("=" * 60)
