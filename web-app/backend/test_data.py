"""诊断 fetch_fund_history_sync 和 fetch_index_history_sync 的数据"""
import sys
sys.path.insert(0, r"d:\软件\Obsidian\创新\AI工具\基金管理工具\web-app\backend")
from main import fetch_fund_history_sync, fetch_index_history_sync

print("=" * 60)
print("004746 基金历史:")
fh = fetch_fund_history_sync("004746", days=25)
print(f"  数量: {len(fh)}")
for h in fh[:5]:
    print(f"  {h}")
print("  ...")
for h in fh[-3:]:
    print(f"  {h}")

print()
print("sh000016 上证50 指数历史:")
idx = fetch_index_history_sync("sh000016", days=25)
print(f"  数量: {len(idx)}")
for h in idx[:5]:
    print(f"  {h}")
print("  ...")
for h in idx[-3:]:
    print(f"  {h}")

print()
print("sh000300 沪深300:")
idx = fetch_index_history_sync("sh000300", days=25)
print(f"  数量: {len(idx)}")
print(f"  前2条: {idx[:2]}")

print()
print("sh000905 中证500:")
idx = fetch_index_history_sync("sh000905", days=25)
print(f"  数量: {len(idx)}")
print(f"  前2条: {idx[:2]}")
