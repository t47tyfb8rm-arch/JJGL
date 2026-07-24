import httpx, json

# 触发一组模拟"盘中"快照
print("=" * 60)
print("测试多时点快照采样")
print("=" * 60)

# 模拟 10:00, 13:00, 14:30 三个时点采集（实际 gsz 已经在 15:00 收盘了，模拟历史场景）
# 我们手动构造不同时间点的"假设估值"，注入到 snapshots
import httpx as hx

# 触发 take endpoint 几次来生成样本（虽然现在 20:25 实际 gsz 是收盘估值）
for t in ["10:00", "13:00", "14:30"]:
    body = {"codes": ["011609", "004746"], "time": t}
    r = hx.post("http://localhost:8000/api/snapshots/take", json=body, timeout=15)
    print(f"  触发 {t}: {r.status_code}")

print()
print("=" * 60)
print("当前快照数据")
print("=" * 60)
r = hx.get("http://localhost:8000/api/snapshots", timeout=10).json()
for code in ["011609", "004746"]:
    samps = r.get(code, {}).get("samples", [])
    print(f"\n--- {code} ({len(samps)} 个快照) ---")
    for s in samps:
        print(f"  {s.get('date')} {s.get('time')}: 估值{s.get('est_change'):+.3f} 实际{s.get('actual_change'):+.3f} 残差{s.get('residual'):+.3f}")
