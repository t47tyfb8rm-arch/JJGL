"""测试新闻情绪分类 - 不含 emoji 避免 PowerShell 转义问题"""
import urllib.request, json, sys
r = urllib.request.urlopen("http://localhost:8000/api/portfolio", timeout=60)
d = json.loads(r.read().decode("utf-8"))
news = d.get("news", [])
print("总数: %d" % len(news))
print()
icon_map = {"bullish": "[+]", "bearish": "[-]", "neutral": "[ ]"}
for n in news[:15]:
    s = n.get("sentiment", "neutral")
    si = icon_map.get(s, "[ ]")
    tags = ",".join(n.get("tags", [])) or "-"
    title = n.get("title", "")[:55]
    print("%s [%s] %s  %s" % (si, tags, n.get("time", ""), title))

# 分类统计
from collections import Counter
sent_counter = Counter(n.get("sentiment", "neutral") for n in news)
tag_counter = Counter()
for n in news:
    for t in n.get("tags", []):
        tag_counter[t] += 1
print()
print("情绪分布:", dict(sent_counter))
print("事件标签分布:", dict(tag_counter))
