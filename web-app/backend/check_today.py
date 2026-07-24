import httpx

codes = ['011609', '020741', '004746']
names = {
    '011609': '易方达科创50联接C',
    '020741': '华泰保兴安悦债券C',
    '004746': '易方达上证50增强C'
}

print("=" * 80)
print("🧮 残差修正模型 - 今日估值对比")
print("=" * 80)

for code in codes:
    r = httpx.get(f'http://localhost:8000/api/funds/{code}', timeout=15.0)
    d = r.json()
    raw = d['estimated_change']
    offset = d['correction_offset']
    corrected = d['corrected_estimated_change']
    actual = d['daily_change']
    n = d['correction_sample_count']
    conf = d['correction_confidence']
    std = d['correction_std']
    est_nav = d['estimated_nav']
    cur_nav = d['current_nav']
    nav_date = d['nav_date']

    print(f"\n【{code} {names[code]}】")
    print(f"  净值日期: {nav_date}  |  实际净值: {cur_nav}")
    print(f"  实际涨跌(20:00后): {actual:+.2f}%")
    print(f"  ─────────────────────────────────────────")
    print(f"  ① 天天基金原始估值:  {raw:+.3f}%   (估净值 {est_nav})")
    print(f"  ② 残差修正系数:      {offset:+.3f}%   (基于 {n} 样本, 置信度 {conf}, σ={std:.3f})")
    print(f"  ③ 修正后估值 (①+②): {corrected:+.3f}%")
    if actual != 0 and raw != 0:
        raw_err = actual - raw
        cor_err = actual - corrected
        print(f"  ─────────────────────────────────────────")
        print(f"  原始估值偏差: {raw_err:+.3f}%")
        print(f"  修正后偏差:   {cor_err:+.3f}%   {'✅ 更准' if abs(cor_err) < abs(raw_err) else '⚠️ 更差'}")

print("\n" + "=" * 80)
print("说明: ③ = ① + ②。修正系数来自历史 (实际 - 天天估值) 的均值，")
print("      反映天天基金 gsz 估值的系统性偏差。")
print("=" * 80)
