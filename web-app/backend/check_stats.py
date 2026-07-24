import httpx, json
r = httpx.get('http://localhost:8000/api/correction-stats', timeout=10.0)
d = r.json()
for c in ['011609', '020741', '004746']:
    s = d[c]
    n = s['sample_count']
    mr = s['mean_residual']
    cf = s['confidence']
    en = 'enabled' if s['enabled'] else 'disabled'
    print(f"{c} ({s['samples'][0]['date'] if s['samples'] else 'no data'}): 样本{n} 均值{mr:+.3f} 置信度{cf} {en}")
    for s2 in s['samples']:
        print(f"    {s2['date']}: 估值{s2['est_change']:+.2f} 实际{s2['actual_change']:+.2f} 残差{s2['residual']:+.2f}")
