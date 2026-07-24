"""手动跑多因子回归"""
import sys
sys.path.insert(0, r"d:\软件\Obsidian\创新\AI工具\基金管理工具\web-app\backend")
import main

print("=" * 60)
print("测试 fit_multi_factor_regression(004746)")
result = main.fit_multi_factor_regression("004746", days=20)
print(f"结果: {result}")
