"""看后端 FundInfo 实际返回的 model_* 字段名"""
import urllib.request, json
for code in ["011609", "020741", "004746"]:
    r = urllib.request.urlopen(f"http://localhost:8000/api/funds/{code}", timeout=10)
    d = json.loads(r.read().decode("utf-8"))
    # 只打印 model 相关字段
    model_keys = {k: v for k, v in d.items() if 'model' in k.lower()}
    print(f"{code}: {json.dumps(model_keys, ensure_ascii=False)}")
    print()
