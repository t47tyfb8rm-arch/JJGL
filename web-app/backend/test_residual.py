"""手动测试 record_residual 对 020741 的容差"""
import sys
sys.path.insert(0, r"d:\软件\Obsidian\创新\AI工具\基金管理工具\web-app\backend")
from main import record_residual, CORRECTION_CACHE
import json

# 模拟 020741 今天的样本：est=0.001, actual=0.06
print("=" * 50)
print("测试 020741: est=0.001, actual=0.06")
print(f"调用前样本数: {len(CORRECTION_CACHE.get('020741', {}).get('samples', []))}")
record_residual("020741", 0.001, 0.06, "2026-07-10", sample_time="manual_test")
print(f"调用后样本数: {len(CORRECTION_CACHE.get('020741', {}).get('samples', []))}")
samples = CORRECTION_CACHE.get("020741", {}).get("samples", [])
for s in samples[-3:]:
    print(f"  {s}")

print()
print("测试 011609: est=-5.53, actual=-5.25")
record_residual("011609", -5.53, -5.25, "2026-07-10", sample_time="manual_test2")
print(f"  011609 样本数: {len(CORRECTION_CACHE.get('011609', {}).get('samples', []))}")
