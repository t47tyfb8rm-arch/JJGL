"""完整看一个 fund 的原始数据"""
import urllib.request, json
r = urllib.request.urlopen("http://localhost:8000/api/funds/011609", timeout=10)
d = json.loads(r.read().decode("utf-8"))
print(json.dumps(d, ensure_ascii=False, indent=2))
