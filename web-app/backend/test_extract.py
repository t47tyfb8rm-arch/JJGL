"""测试新闻正文提取"""
import urllib.request, json, urllib.parse
r = urllib.request.urlopen("http://localhost:8000/api/portfolio", timeout=60)
d = json.loads(r.read().decode("utf-8"))
news = d.get("news", [])
if not news:
    print("no news")
else:
    url = news[0].get("url", "")
    title = news[0].get("title", "")
    print("测试新闻: %s" % title)
    print("URL: %s" % url)
    print()
    encoded = urllib.parse.quote(url, safe="")
    api = "http://localhost:8000/api/news-content?url=" + encoded
    try:
        r2 = urllib.request.urlopen(api, timeout=20)
        out = json.loads(r2.read().decode("utf-8"))
        print("ok: %s" % out.get("ok"))
        print("提取title: %s" % out.get("title", "")[:80])
        print("提取日期: %s" % out.get("date", ""))
        print("正文字符数: %s" % out.get("length", 0))
        print("---前500字---")
        print(out.get("content", "")[:500])
    except Exception as e:
        print("ERR: %s" % e)
