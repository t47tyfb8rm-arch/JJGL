"""恢复 7/10 est_cache 到 7/11，让周末保持前一个交易时间预估"""
import json
import os
from datetime import datetime

cache_file = r"d:\软件\Obsidian\创新\AI工具\基金管理工具\web-app\backend\est_cache.json"
with open(cache_file, "r", encoding="utf-8") as f:
    cache = json.load(f)

# 7/10 实际 gsz 值（从历史记录恢复）
restore = {
    "011609": {"est_nav": 1.5414, "est_change": -5.53, "est_time": "2026-07-10 15:00"},
    "004746": {"est_nav": 0.8807, "est_change": -1.34, "est_time": "2026-07-10 15:00"},
    "020741": {"est_nav": 1.1476, "est_change": 0.001, "est_time": "2026-07-10 15:00"},
}
for code, val in restore.items():
    if code not in cache:
        cache[code] = val
        print(f"✓ 恢复 {code} → {val}")
    else:
        print(f"- {code} 已有缓存，跳过")

# 标记缓存数据对应的日期（不是当天）
cache["_date"] = datetime.now().strftime("%Y-%m-%d")
cache["_est_origin_date"] = "2026-07-10"  # 标记来源

with open(cache_file, "w", encoding="utf-8") as f:
    json.dump(cache, f, ensure_ascii=False, indent=2)
print("\n✓ est_cache.json 已更新")
