import urllib.request, json
codes = ["011609", "020741", "004746"]
for code in codes:
    try:
        r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=10)
        f = json.loads(r.read().decode("utf-8"))
        print(f"=== {code} {f.get('name')}")
        print(f"  model_type        = {f.get('model_type')}")
        print(f"  model_bench_name  = {f.get('model_benchmark_name')}")
        print(f"  model_bench_chg   = {f.get('model_benchmark_change')}")
        print(f"  model_est         = {f.get('model_estimated_change')}")
        print(f"  model_offset      = {f.get('model_offset')}")
        print(f"  model_std         = {f.get('model_std')}")
        print(f"  model_enabled     = {f.get('model_enabled')}")
        print(f"  model_conf        = {f.get('model_confidence')} (n={f.get('model_sample_count')})")
        print(f"  gsz est           = {f.get('estimated_change')}")
        print(f"  corrected est     = {f.get('corrected_estimated_change')}")
        print(f"  actual daily      = {f.get('daily_change')}")
        print(f"  est_nav           = {f.get('estimated_nav')}")
    except Exception as e:
        print(f"{code}: {e}")
    print()
