"""更精确的时间分析"""
import urllib.request, json, time

for code in ["011609", "020741", "004746"]:
    # 第一次
    t0 = time.time()
    r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=60)
    data = json.loads(r.read().decode("utf-8"))
    dt1 = time.time() - t0
    # 第二次（缓存生效）
    t0 = time.time()
    r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=60)
    data2 = json.loads(r.read().decode("utf-8"))
    dt2 = time.time() - t0
    print(f"{code}: 1st={dt1:.2f}s, 2nd={dt2:.2f}s, model_est={data.get('model_estimated_change')}")
