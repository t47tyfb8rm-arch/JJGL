"""诊断哪个步骤慢"""
import urllib.request, json, time

for code in ["011609", "020741", "004746"]:
    t0 = time.time()
    try:
        r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=30)
        data = json.loads(r.read().decode("utf-8"))
        dt = time.time() - t0
        print(f"{code} OK ({dt:.2f}s): name={data.get('name')}, est={data.get('estimated_change')}, model={data.get('model_type')}, model_est={data.get('model_estimated_change')}, model_bench={data.get('model_benchmark_change')}, model_name={data.get('model_benchmark_name')}")
    except Exception as e:
        dt = time.time() - t0
        print(f"{code} ERR ({dt:.2f}s): {e}")
