import httpx

codes = ['011609', '020741', '004746']
names = {
    '011609': '易方达科创50联接C',
    '020741': '华泰保兴安悦债券C',
    '004746': '易方达上证50增强C'
}

print("=" * 80)
print("🧬 基金专用模型估值对比")
print("=" * 80)

for code in codes:
    r = httpx.get(f'http://localhost:8000/api/funds/{code}', timeout=20)
    d = r.json()
    actual = d['daily_change']
    gsz = d['estimated_change']
    gsz_corrected = d['corrected_estimated_change']
    model_est = d['model_estimated_change']
    model_bench = d['model_benchmark_change']
    model_name = d['model_benchmark_name']
    model_type = d['model_type']
    model_offset = d['model_offset']
    model_samples = d['model_sample_count']
    model_conf = d['model_confidence']
    model_enabled = d['model_enabled']

    print(f"\n【{code} {names[code]}】")
    print(f"  模型类型: {model_type}")
    if model_type != 'residual_only':
        print(f"  基准: {model_name} (实时 {model_bench:+.3f}%)")
        print(f"  残差均值: {model_offset:+.3f}% (基于 {model_samples} 样本, 置信度 {model_conf}, {'✅ enabled' if model_enabled else '⏳ 学习中'})")
        print(f"  ─────────────────────────────────────────")
        print(f"  实际涨跌:                       {actual:+.3f}%")
        print(f"  ① 天天基金 gsz 估值:            {gsz:+.3f}%")
        print(f"  ② 残差修正后 gsz:               {gsz_corrected:+.3f}%")
        print(f"  ③ 基金专用模型估值:             {model_est:+.3f}%")
        if actual != 0 and model_est != 0:
            err_gsz = actual - gsz
            err_corr = actual - gsz_corrected
            err_model = actual - model_est
            best = min([('①gsz', abs(err_gsz)), ('②残差', abs(err_corr)), ('③模型', abs(err_model))], key=lambda x: x[1])
            print(f"  ─────────────────────────────────────────")
            print(f"  ① 偏差: {err_gsz:+.3f}%")
            print(f"  ② 偏差: {err_corr:+.3f}%")
            print(f"  ③ 偏差: {err_model:+.3f}%")
            print(f"  🏆 最佳: {best[0]} (偏差 {best[1]:.3f}%)")
    else:
        print(f"  📌 维持 gsz 残差修正（gsz 精度限制 / 债券基金）")
        print(f"  实际: {actual:+.3f}% | 估值: {gsz:+.3f}%")

print("\n" + "=" * 80)
print("💡 指数模型优势:")
print("  • 科创50/上证50 指数盘中实时更新，比天天基金 gsz 准得多")
print("  • 残差 = fund_actual - 指数_actual，自动学习基金对指数的偏离")
print("  • 样本 ≥ 3 后启用修正，≥ 5 后高置信")
print("=" * 80)
