"""验证 index.html 服务的代码是不是最新的"""
import urllib.request
r = urllib.request.urlopen("http://localhost:8000/", timeout=10)
text = r.read().decode("utf-8")
# 查找每个基金 chip 标签
for kw in ["指数跟随", "多因子加权", "债基基线", "gsz残差修正", "gsz残差学习"]:
    print(f"'{kw}': 出现 {text.count(kw)} 次")
print()
# 看看三个基金的 actual 数据中 model_type 是什么
import json
for code in ["011609", "020741", "004746"]:
    r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=10)
    d = json.loads(r.read().decode("utf-8"))
    print(f"{code} {d['name']}: model_type={d.get('model_type')}, model_est={d.get('model_estimated_change')}, model_bench_name={d.get('model_benchmark_name')}, model_enabled={d.get('model_enabled')}")
