"""直接测试 fetch_realtime_index"""
import asyncio
import sys
sys.path.insert(0, r"d:\软件\Obsidian\创新\AI工具\基金管理工具\web-app\backend")
import main

async def test():
    for code in ["sh000688", "sh000016", "sh000300", "sh000905"]:
        data = await main.fetch_realtime_index(code)
        print(f"{code}: {data}")

asyncio.run(test())
