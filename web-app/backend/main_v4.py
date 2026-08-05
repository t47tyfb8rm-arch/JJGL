"""
基金管理系统 - FastAPI 后端
从天天基金网和新浪财经获取实时数据
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse as StarletteFileResponse
from starlette.types import Scope
import stat

class NoCacheStaticFiles(StaticFiles):
    """静态文件挂载，强制禁止缓存"""
    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
from pydantic import BaseModel
from typing import List, Optional, Dict
import httpx
from datetime import datetime, timedelta
import re
import asyncio
import os
import json
import time
import sqlite3
from urllib.parse import quote

app = FastAPI(title="基金管理系统", version="2.3.2")

# === 盘中快照定时采样后台任务 ===
SNAPSHOT_FUND_CODES = ["011609", "020741", "004746"]


async def snapshot_scheduler():
    """后台调度：每个 A 股交易日 10:05 / 13:05 / 14:32 触发，对所有基金采集 gsz 估值"""
    import asyncio as _asyncio
    # 在每个采样点后的第 2-5 分钟触发（确保 gsz 接口已更新）
    fire_minutes = {10: 5, 13: 5, 14: 32}
    last_fired_date = None
    last_fired_slot = None
    while True:
        try:
            now = datetime.now()
            # 仅 A 股交易日
            if is_trading_day(now) and now.hour in fire_minutes:
                slot_key = (now.date(), now.hour, fire_minutes[now.hour])
                if now.minute == fire_minutes[now.hour] and slot_key != last_fired_slot:
                    last_fired_slot = slot_key
                    snapshot_time = f"{now.hour:02d}:{now.minute:02d}"
                    trade_date = now.strftime("%Y-%m-%d")
                    print(f"[调度] 触发 {trade_date} {snapshot_time} 盘中快照采样")
                    for code in SNAPSHOT_FUND_CODES:
                        await take_intraday_snapshot(code, snapshot_time)
                    last_fired_date = now.date()
        except Exception as e:
            print(f"[调度] 异常: {e}")
        await _asyncio.sleep(30)  # 每 30 秒检查一次


async def daily_model_fitter():
    """每天 20:30 自动重跑多因子回归 + 指数残差统计
    启动时也跑一次（冷启动 / 重启恢复）"""
    import asyncio as _asyncio
    last_run_date = None
    while True:
        try:
            now = datetime.now()
            today = now.date()
            # 20:30 后跑一次/天
            if now.hour >= 20 and now.minute >= 30 and last_run_date != today:
                print(f"[自学习] 每日 20:30 模型自学习开始")
                for code, cfg in FUND_SPECIFIC_MODELS.items():
                    if cfg.get("type") == "multi_factor":
                        try:
                            fit_multi_factor_regression(code, days=20)
                        except Exception as e:
                            print(f"[自学习] {code} 回归失败: {e}")
                last_run_date = today
        except Exception as e:
            print(f"[自学习] 异常: {e}")
        await _asyncio.sleep(60)


@app.on_event("startup")
async def start_snapshot_scheduler():
    """启动后台调度任务"""
    import asyncio as _asyncio
    init_app_db()
    _asyncio.create_task(snapshot_scheduler())
    _asyncio.create_task(daily_model_fitter())
    _asyncio.create_task(background_portfolio_refresher())
    print(f"[启动] 盘中快照调度已启动，采样时点: {SNAPSHOT_TIMES}")
    # 启动时立即跑一次多因子回归（冷启动 / 重启恢复）
    import asyncio as _asyncio2
    async def _initial_fit():
        await _asyncio2.sleep(2)
        for code, cfg in FUND_SPECIFIC_MODELS.items():
            if cfg.get("type") == "multi_factor":
                try:
                    fit_multi_factor_regression(code, days=20)
                except Exception as e:
                    print(f"[启动] {code} 初始回归失败: {e}")
    _asyncio2.create_task(_initial_fit())



def is_trading_time() -> bool:
    """判断当前是否为A股交易时间（工作日 9:30-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if not is_trading_day():
        return False
    morning_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    afternoon_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    return morning_start <= now <= afternoon_end


def market_status() -> str:
    """
    市场状态（业务语义）：
    - "trading": 工作日 9:30-15:00 → 盘中，估值在动，需要 30s 拉一次
    - "closed": 其他时间（9:30 前 / 15:00 后 / 周末 / 节假日）
                  → 基金当日净值已定（15:00 后披露），到下一交易日 9:30 前都没新数据
                  → 整页缓存命中即可
    """
    return "trading" if is_trading_time() else "closed"


def is_post_market() -> bool:
    """判断当前是否为盘末（工作日 15:00 后到次日 9:30 前）
    盘末特征：当日基金净值已定（15:00 后陆续披露），无"盘中估值"概念
    - AI 综合分析只用 15:00 那一刻的快照数据（daily_change 实际涨跌）
    - est_change 强制视为 0（盘末没有"实时估值"语义）
    """
    if not is_trading_day():
        return True  # 周末/节假日也算盘末
    now = datetime.now()
    cutoff = now.replace(hour=15, minute=0, second=0, microsecond=0)
    return now >= cutoff


def should_fetch_estimation() -> bool:
    """判断是否应该获取/使用估算数据（盘中 9:30-15:00）
    盘中实时拉 fundgz 估算净值；盘末（15:00 后）不再拉，est 视为无效
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if is_post_market():
        return False  # 盘末不 fetch est（daily_change 才是真相）
    morning_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return now >= morning_start


# 获取当前目录（backend）
current_dir = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = current_dir
DB_PATH = os.environ.get("FUND_MANAGER_DB_PATH", os.path.join(current_dir, "fund_manager_v4.db"))


def init_app_db():
    os.makedirs(current_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_key TEXT NOT NULL DEFAULT 'latest',
                created_at REAL NOT NULL,
                trade_date TEXT,
                market_status TEXT,
                payload TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_key_created ON portfolio_snapshots(snapshot_key, created_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fund_daily_snapshots (
                code TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                name TEXT,
                nav_date TEXT,
                current_nav REAL,
                daily_change REAL,
                estimated_change REAL,
                buy_point_json TEXT,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(code, snapshot_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_snapshots (
                news_key TEXT NOT NULL DEFAULT 'latest',
                created_at REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(news_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_nav_snapshots (
                code TEXT NOT NULL,
                created_at REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fund_nav_history (
                code TEXT NOT NULL,
                nav_date TEXT NOT NULL,
                nav REAL,
                daily_change REAL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(code, nav_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_estimate_snapshots (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                estimate_time TEXT NOT NULL,
                estimated_change REAL,
                model_estimated_change REAL,
                corrected_estimated_change REAL,
                benchmark_change REAL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(code, trade_date, estimate_time)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_snapshots (
                index_key TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                current REAL,
                daily_change REAL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(index_key, trade_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS theme_sector_snapshots (
                name TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                value REAL,
                tone TEXT,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(name, snapshot_date, source)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS external_market_snapshots (
                name TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                value REAL,
                tone TEXT,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(name, snapshot_date, source)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_strategy_snapshots (
                code TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                risk_level TEXT,
                trend TEXT,
                advice TEXT,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(code, snapshot_date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_nav_history_code_date ON fund_nav_history(code, nav_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intraday_estimates_code_time ON intraday_estimate_snapshots(code, trade_date DESC, estimate_time DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_index_snapshots_key_date ON index_snapshots(index_key, trade_date DESC)")
        conn.commit()


def _model_dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj.dict()


def _json_payload(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def save_cost_navs_to_db(cost_navs: Dict[str, dict]):
    try:
        init_app_db()
        now_ts = time.time()
        with sqlite3.connect(DB_PATH) as conn:
            for code, value in (cost_navs or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO cost_nav_snapshots(code, created_at, payload) VALUES(?,?,?)",
                    (str(code), now_ts, _json_payload(value if isinstance(value, dict) else {"value": value})),
                )
            conn.commit()
    except Exception as e:
        print(f"[DB] save cost navs failed: {e}")


def save_portfolio_to_db(response):
    try:
        init_app_db()
        payload = _json_payload(_model_dump(response))
        now_ts = time.time()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots(snapshot_key, created_at, trade_date, market_status, payload) VALUES(?,?,?,?,?)",
                ("latest", now_ts, response.date, response.market_status, payload),
            )
            conn.execute(
                "DELETE FROM portfolio_snapshots WHERE snapshot_key='latest' AND id NOT IN (SELECT id FROM portfolio_snapshots WHERE snapshot_key='latest' ORDER BY created_at DESC LIMIT 24)"
            )
            for fund in response.funds or []:
                fund_dict = _model_dump(fund)
                fund_payload = _json_payload(fund_dict)
                buy_point_payload = _json_payload(_model_dump(fund.buy_point)) if getattr(fund, "buy_point", None) else "{}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO fund_daily_snapshots
                    (code, snapshot_date, name, nav_date, current_nav, daily_change, estimated_change, buy_point_json, payload, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fund.code,
                        response.date,
                        fund.name,
                        fund.nav_date,
                        fund.current_nav,
                        fund.daily_change,
                        fund.estimated_change,
                        buy_point_payload,
                        fund_payload,
                        now_ts,
                    ),
                )
                if getattr(fund, "nav_date", ""):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO fund_nav_history
                        (code, nav_date, nav, daily_change, payload, updated_at)
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            fund.code,
                            fund.nav_date,
                            fund.current_nav,
                            fund.daily_change,
                            fund_payload,
                            now_ts,
                        ),
                    )
                if getattr(fund, "estimated_time", "") or getattr(fund, "estimated_change", None) is not None:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO intraday_estimate_snapshots
                        (code, trade_date, estimate_time, estimated_change, model_estimated_change, corrected_estimated_change, benchmark_change, payload, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            fund.code,
                            response.date,
                            getattr(fund, "estimated_time", "") or datetime.now().strftime("%H:%M:%S"),
                            getattr(fund, "estimated_change", None),
                            getattr(fund, "model_estimated_change", None),
                            getattr(fund, "corrected_estimated_change", None),
                            getattr(fund, "model_benchmark_change", None),
                            fund_payload,
                            now_ts,
                        ),
                    )
                if getattr(fund, "ai_prediction", None):
                    ai = fund.ai_prediction
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO ai_strategy_snapshots
                        (code, snapshot_date, risk_level, trend, advice, payload, updated_at)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            fund.code,
                            response.date,
                            getattr(ai, "risk_level", ""),
                            getattr(ai, "trend", ""),
                            getattr(ai, "advice", ""),
                            _json_payload(_model_dump(ai)),
                            now_ts,
                        ),
                    )
            for index_key, index_obj in [
                ("shanghai", getattr(response, "index", None)),
                ("bond", getattr(response, "bond_index", None)),
                ("k50", getattr(response, "k50_index", None)),
                ("hsi", getattr(response, "hsi_index", None)),
                ("hs300", getattr(response, "hs300_index", None)),
                ("shenzhen", getattr(response, "sz_index", None)),
            ]:
                if index_obj:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO index_snapshots
                        (index_key, trade_date, current, daily_change, payload, updated_at)
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            index_key,
                            response.date,
                            getattr(index_obj, "current", None),
                            getattr(index_obj, "daily_change", None),
                            _json_payload(_model_dump(index_obj)),
                            now_ts,
                        ),
                    )
            for sector in response.theme_sectors or []:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO theme_sector_snapshots
                    (name, snapshot_date, source, value, tone, payload, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        sector.name,
                        response.date,
                        getattr(sector, "source", "") or "",
                        getattr(sector, "value", None),
                        getattr(sector, "tone", ""),
                        _json_payload(_model_dump(sector)),
                        now_ts,
                    ),
                )
            for market in response.external_markets or []:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO external_market_snapshots
                    (name, snapshot_date, source, value, tone, payload, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        market.name,
                        response.date,
                        getattr(market, "source", "") or "",
                        getattr(market, "value", None),
                        getattr(market, "tone", ""),
                        _json_payload(_model_dump(market)),
                        now_ts,
                    ),
                )
            for code, value in (COST_NAVS or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO cost_nav_snapshots(code, created_at, payload) VALUES(?,?,?)",
                    (str(code), now_ts, _json_payload(value if isinstance(value, dict) else {"value": value})),
                )
            news_payload = _json_payload([_model_dump(n) for n in response.news or []])
            conn.execute(
                "INSERT OR REPLACE INTO news_snapshots(news_key, created_at, payload) VALUES(?,?,?)",
                ("latest", now_ts, news_payload),
            )
            conn.execute("DELETE FROM intraday_estimate_snapshots WHERE updated_at < ?", (now_ts - 86400 * 10,))
            conn.execute("DELETE FROM index_snapshots WHERE updated_at < ?", (now_ts - 86400 * 90,))
            conn.execute("DELETE FROM theme_sector_snapshots WHERE updated_at < ?", (now_ts - 86400 * 90,))
            conn.execute("DELETE FROM external_market_snapshots WHERE updated_at < ?", (now_ts - 86400 * 90,))
            conn.commit()
    except Exception as e:
        print(f"[DB] save portfolio failed: {e}")


def load_portfolio_from_db(max_age_seconds: int = 180):
    try:
        init_app_db()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT created_at, payload FROM portfolio_snapshots WHERE snapshot_key='latest' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        created_at, payload = row
        if max_age_seconds > 0 and time.time() - float(created_at) > max_age_seconds:
            return None
        data = json.loads(payload)
        if hasattr(PortfolioResponse, "model_validate"):
            return PortfolioResponse.model_validate(data)
        return PortfolioResponse.parse_obj(data)
    except Exception as e:
        print(f"[DB] load portfolio failed: {e}")
        return None


def load_portfolio_snapshot_from_db():
    try:
        init_app_db()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT created_at, payload FROM portfolio_snapshots WHERE snapshot_key='latest' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None, None
        created_at, payload = row
        data = json.loads(payload)
        response = PortfolioResponse.model_validate(data) if hasattr(PortfolioResponse, "model_validate") else PortfolioResponse.parse_obj(data)
        return response, time.time() - float(created_at)
    except Exception as e:
        print(f"[DB] load snapshot failed: {e}")
        return None, None


def load_index_history_from_db(index_key: str, limit: int = 12) -> List["IndexHistoryItem"]:
    try:
        init_app_db()
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT trade_date, current, daily_change, payload
                FROM index_snapshots
                WHERE index_key=?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (index_key, max(1, int(limit))),
            ).fetchall()
        history: List[IndexHistoryItem] = []
        for trade_date, current, daily_change, payload in rows:
            close = current
            change = daily_change
            try:
                data = json.loads(payload or "{}")
                close = data.get("current", close)
                change = data.get("daily_change", change)
            except Exception:
                pass
            try:
                if close is None or float(close) <= 0:
                    continue
                history.append(IndexHistoryItem(
                    date=str(trade_date)[:10],
                    close=round(float(close), 2),
                    change=round(float(change or 0.0), 2),
                ))
            except Exception:
                continue
        return normalize_index_history(history, limit=limit)
    except Exception as e:
        print(f"[DB] load index history failed: {index_key} {e}")
        return []


def clear_portfolio_db_cache():
    try:
        init_app_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM portfolio_snapshots WHERE snapshot_key='latest'")
            conn.commit()
    except Exception as e:
        print(f"[DB] clear portfolio failed: {e}")

EST_CACHE_FILE = os.path.join(current_dir, "est_cache.json")
CORRECTION_CACHE_FILE = os.path.join(current_dir, "correction_cache.json")
# === 盘中估值快照：每天 10:00 / 13:00 / 14:30 三个时点的 gsz 估值 ===
INTRADAY_SNAPSHOTS_FILE = os.path.join(current_dir, "intraday_snapshots.json")
# 采样时点（HH:MM）
SNAPSHOT_TIMES = ["10:00", "13:00", "14:30"]
# === A 股交易日历 ===
TRADING_CALENDAR_FILE = os.path.join(current_dir, "trading_calendar.json")

# === 基金专用模型配置 ===
# 每只基金单独设计最优模型
FUND_SPECIFIC_MODELS = {
    "011609": {  # 易方达上证科创50联接C（被动指数联接基金）
        "type": "index_following",
        "benchmark_code": "sh000688",  # 上证科创板50成份指数
        "benchmark_name": "科创50",
        "description": "被动跟踪科创50指数，残差 = fund_actual - 科创50_actual"
    },
    "004746": {  # 易方达上证50增强C（增强型指数）
        "type": "multi_factor",
        "benchmark_name": "上证50增强代理",
        "factors": [
            {"code": "sh000016", "name": "上证50", "default_weight": 0.62},
            {"code": "sh000300", "name": "沪深300", "default_weight": 0.22},
            {"code": "sh000922", "name": "中证红利", "default_weight": 0.10},
            {"code": "sz399986", "name": "中证银行", "default_weight": 0.06},
        ],
        "description": "增强型指数基金：上证50为主，叠加宽基、红利和银行风格，收盘后用历史误差校准"
    },
    "020741": {  # 华泰保兴安悦债券C（纯债基金）
        "type": "bond_baseline",
        "baseline_window": 10,
        "short_window": 3,
        "description": "纯债趋势：近3日/10日自身净值趋势为主，国债指数只做小权重修正"
    }
}

# 实时指数缓存 {code: (timestamp, data)}，60 秒复用
REALTIME_INDEX_CACHE = {}
REALTIME_INDEX_TTL = 60  # 秒
EXTERNAL_MARKET_CACHE = {"data": None, "saved_at": 0.0}
EXTERNAL_MARKET_CACHE_TTL = 300  # external-market-refresh-5min-20260729: 外部市场后台刷新周期 5 分钟
# 指数残差样本（独立于 gsz 残差）
INDEX_RESIDUAL_FILE = os.path.join(current_dir, "index_residual_cache.json")
INDEX_RESIDUAL_CACHE = {}

# 多因子回归系数缓存（每天 20:00 后用历史 N 天基金+指数实际涨跌拟合）
# 结构: { "004746": {"alpha": -0.01, "weights": {"sh000016": 0.82, "sh000300": 0.11, "sh000905": 0.05},
#                   "r_squared": 0.95, "sample_days": 20, "last_update": "2026-07-10"} }
MULTI_FACTOR_FILE = os.path.join(current_dir, "multi_factor_regression.json")
MULTI_FACTOR_REGRESSION = {}

# 2026 年 A 股法定休市日（基于历史规律估算）
A_SHARE_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",       # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",  # 春节
    "2026-02-24",
    "2026-04-04", "2026-04-05", "2026-04-06",       # 清明
    "2026-05-01", "2026-05-02", "2026-05-03",
    "2026-05-04", "2026-05-05",                      # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",       # 端午
    "2026-09-25", "2026-09-26", "2026-09-27",       # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03",
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
    "2026-10-08",
}


def load_trading_calendar() -> dict:
    """加载交易日历配置
    结构: {"holidays": ["2026-01-01", ...], "extra_workdays": ["2026-02-14", ...]}
    """
    if not os.path.exists(TRADING_CALENDAR_FILE):
        return {"holidays": sorted(list(A_SHARE_HOLIDAYS_2026)), "extra_workdays": []}
    try:
        with open(TRADING_CALENDAR_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"holidays": sorted(list(A_SHARE_HOLIDAYS_2026)), "extra_workdays": []}


def save_trading_calendar(data: dict):
    try:
        with open(TRADING_CALENDAR_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def is_trading_day(d: Optional[datetime] = None) -> bool:
    """判断指定日期是否为 A 股交易日：非周末 且 不在 holidays 列表中"""
    if d is None:
        d = datetime.now()
    if d.weekday() >= 5:  # 周六周日
        return False
    date_str = d.strftime("%Y-%m-%d")
    cal = load_trading_calendar()
    if date_str in cal.get("holidays", []):
        return False
    return True


# ============= 实时指数 + 基金专用模型 =============

def load_index_residual_cache() -> dict:
    """加载指数残差缓存（独立于 gsz 残差）
    结构:
    {
      "_meta": {"max_samples": 60},
      "011609": {
        "samples": [{"date":"2026-07-10","benchmark_change":-5.53,"actual_change":-5.25,"residual":+0.28}],
        "mean_residual": ..., "std_residual": ..., "sample_count": ...
      }
    }
    """
    if not os.path.exists(INDEX_RESIDUAL_FILE):
        return {"_meta": {"max_samples": 60}}
    try:
        with open(INDEX_RESIDUAL_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"_meta": {"max_samples": 60}}


def save_index_residual_cache(data: dict):
    try:
        with open(INDEX_RESIDUAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def fetch_realtime_index(code: str) -> dict:
    """从腾讯财经接口获取实时指数数据
    返回: {name, current, previous, change_amt, change_pct, time_str}
    60 秒内复用缓存
    """
    import time
    now = time.time()
    if code in REALTIME_INDEX_CACHE:
        ts, data = REALTIME_INDEX_CACHE[code]
        if now - ts < REALTIME_INDEX_TTL and data:
            return data
    try:
        url = f"https://qt.gtimg.cn/q={code}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text.strip()
        m = re.search(r'="([^"]+)"', text)
        if not m:
            return {}
        parts = m.group(1).split('~')
        if len(parts) < 33:
            return {}
        # 关键字段位置：name=1, code=2, current=3, previous=4, change_amt=31, change_pct=32
        result = {
            "code": code,
            "name": parts[1],
            "current": float(parts[3]),
            "previous": float(parts[4]),
            "change_amt": float(parts[31]),
            "change_pct": float(parts[32]),
            "time_str": parts[30] if len(parts) > 30 else ""
        }
        REALTIME_INDEX_CACHE[code] = (now, result)
        return result
    except Exception as e:
        print(f"[实时指数] {code} 获取失败: {e}")
        return {}

def _index_secid(code: str) -> str:
    clean = str(code or "").strip().lower()
    if clean in ("hkhsi", "hsi", "hk.hsi"):
        return "100.HSI"
    if clean.startswith("sh"):
        return "1." + clean[2:]
    if clean.startswith("sz"):
        return "0." + clean[2:]
    if clean.startswith("6") or clean.startswith("0"):
        return "1." + clean
    return "0." + clean

def _em_scaled(value, scale: float = 100.0) -> float:
    if value in (None, "", "-"):
        return 0.0
    try:
        return float(value) / scale
    except Exception:
        return 0.0

async def fetch_eastmoney_realtime_index(code: str, name: str = "") -> dict:
    """Eastmoney realtime index quote, used before Tencent/K-line for intraday index cards."""
    cache_key = f"em:{code}"
    now = time.time()
    if cache_key in REALTIME_INDEX_CACHE:
        ts, data = REALTIME_INDEX_CACHE[cache_key]
        if now - ts < REALTIME_INDEX_TTL and data:
            return data
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": _index_secid(code),
            "fields": "f43,f57,f58,f60,f169,f170",
            "_": int(now * 1000),
        }
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers={**HTTP_HEADERS, "Referer": "https://quote.eastmoney.com/"})
            r.raise_for_status()
            data = r.json().get("data") or {}
        current = _em_scaled(data.get("f43"))
        previous = _em_scaled(data.get("f60"))
        change_amt = _em_scaled(data.get("f169"))
        change_pct = _em_scaled(data.get("f170"))
        if current <= 0:
            return {}
        result = {
            "code": code,
            "name": name or data.get("f58") or code,
            "current": current,
            "previous": previous,
            "change_amt": change_amt,
            "change_pct": change_pct,
            "time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "eastmoney",
        }
        REALTIME_INDEX_CACHE[cache_key] = (now, result)
        return result
    except Exception as e:
        print(f"[东方财富实时指数] {code} 获取失败: {e}")
        return {}

def record_index_residual(fund_code: str, benchmark_change: float, actual_change: float, trade_date: str):
    """记录一条指数残差样本
    residual = actual - benchmark，正值表示基金跑赢基准
    """
    if abs(benchmark_change) < 0.01 or actual_change == 0:
        return
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    fund_entry = INDEX_RESIDUAL_CACHE.setdefault(fund_code, {"samples": []})
    samples = fund_entry.setdefault("samples", [])
    # 同一天去重
    samples = [s for s in samples if s.get("date") != trade_date]
    samples.append({
        "date": trade_date,
        "benchmark_change": round(benchmark_change, 3),
        "actual_change": round(actual_change, 3),
        "residual": round(actual_change - benchmark_change, 3)
    })
    max_n = INDEX_RESIDUAL_CACHE.get("_meta", {}).get("max_samples", 60)
    samples = samples[-max_n:]
    fund_entry["samples"] = samples
    # 统计
    residuals = [s["residual"] for s in samples]
    n = len(residuals)
    mean_r = round(sum(residuals) / n, 4) if n > 0 else 0.0
    if n >= 2:
        var_r = sum((x - mean_r) ** 2 for x in residuals) / (n - 1)
        std_r = round(var_r ** 0.5, 4)
    else:
        std_r = 0.0
    if n >= 5:
        confidence = "high"
    elif n >= 3:
        confidence = "medium"
    elif n >= 1:
        confidence = "low"
    else:
        confidence = "none"
    fund_entry["mean_residual"] = mean_r
    fund_entry["std_residual"] = std_r
    fund_entry["sample_count"] = n
    fund_entry["confidence"] = confidence
    fund_entry["last_residual"] = residuals[-1] if residuals else 0
    fund_entry["last_update"] = trade_date
    save_index_residual_cache(INDEX_RESIDUAL_CACHE)


def get_index_model_estimate(fund_code: str) -> dict:
    """根据基金专用模型计算指数残差修正估值
    返回: {enabled, model_type, estimated_change, benchmark_change, offset, sample_count, confidence}
    """
    model = FUND_SPECIFIC_MODELS.get(fund_code, {})
    model_type = model.get("type", "residual_only")
    stats = INDEX_RESIDUAL_CACHE.get(fund_code, {})
    n = stats.get("sample_count", 0)
    confidence = stats.get("confidence", "none")
    mean_r = stats.get("mean_residual", 0.0)
    enabled = n >= 3 and confidence in ("medium", "high")
    return {
        "model_type": model_type,
        "enabled": enabled,
        "offset": mean_r if enabled else 0.0,
        "std": stats.get("std_residual", 0.0),
        "sample_count": n,
        "confidence": confidence
    }


def bond_curve_proxy_signal(bond_index: Optional["IndexInfo"]) -> dict:
    """国债曲线代理信号。

    没有稳定的实时收益率曲线接口时，用国债指数近几日走势模拟利率曲线方向：
    - 当日变化代表当天方向
    - 近3日/5日均值代表短期利率趋势
    - 波动过大时降低信号强度，避免债基估算被单日噪声带偏
    """
    if not bond_index or bond_index.current <= 0:
        return {"signal": 0.0, "latest": 0.0, "momentum": 0.0, "vol": 0.0, "quality": "无数据"}

    changes = [float(bond_index.daily_change or 0.0)]
    for h in (bond_index.history or []):
        try:
            c = float(h.change)
        except Exception:
            continue
        if abs(c) <= 0.5:
            changes.append(c)

    clean = [c for c in changes if abs(c) >= 0.0001][:7]
    if not clean:
        return {"signal": 0.0, "latest": 0.0, "momentum": 0.0, "vol": 0.0, "quality": "弱"}

    latest = clean[0]
    avg3 = sum(clean[:min(3, len(clean))]) / min(3, len(clean))
    avg5 = sum(clean[:min(5, len(clean))]) / min(5, len(clean))
    mean = sum(clean) / len(clean)
    vol = (sum((x - mean) ** 2 for x in clean) / max(1, len(clean) - 1)) ** 0.5 if len(clean) >= 2 else 0.0

    raw = latest * 0.50 + avg3 * 0.35 + avg5 * 0.15
    damp = 0.65 if vol > 0.08 else (0.80 if vol > 0.05 else 1.0)
    signal = round(max(-0.16, min(0.16, raw * damp)), 3)
    quality = "强" if len(clean) >= 5 and vol <= 0.05 else ("中" if len(clean) >= 3 else "弱")
    return {
        "signal": signal,
        "latest": round(latest, 3),
        "momentum": round(avg3, 3),
        "vol": round(vol, 4),
        "quality": quality
    }


def build_bond_range_estimate(
    fund_code: str,
    model_estimated_change: float,
    bond_index: Optional["IndexInfo"],
    curve_proxy: Optional[dict],
    beta: float,
    r_squared: float,
) -> dict:
    """Build a conservative display model for pure bond funds.

    Bond fund daily moves are often only a few bps, so a single precise number
    is misleading. The UI uses this range plus signal wording before NAV is
    disclosed, while actual NAV still wins after disclosure.
    """
    if fund_code != "020741":
        return {}

    base = float(model_estimated_change or 0.0)
    idx_change = float(getattr(bond_index, "daily_change", 0.0) or 0.0) if bond_index else 0.0
    curve = curve_proxy or bond_curve_proxy_signal(bond_index)
    curve_signal = float(curve.get("signal", 0.0) or 0.0)
    momentum = float(curve.get("momentum", 0.0) or 0.0)
    quality = str(curve.get("quality", "弱") or "弱")

    # Keep the direction conservative: the fund's own recent NAV trend is the
    # anchor, long-bond proxy only nudges the signal.
    center = round(base * 0.76 + curve_signal * float(beta or 0.5) * 0.18 + idx_change * 0.06, 3)
    center = max(-0.16, min(0.16, center))

    spread = 0.025
    if quality == "弱":
        spread += 0.015
    if r_squared < 0.25:
        spread += 0.010
    spread += min(0.025, abs(momentum) * 0.18)

    low = round(max(-0.18, center - spread), 3)
    high = round(min(0.18, center + spread), 3)
    if low > high:
        low, high = high, low

    if center >= 0.035:
        signal, tone = "偏暖", "good"
    elif center <= -0.035:
        signal, tone = "偏弱", "bad"
    else:
        signal, tone = "偏稳", "neutral"

    if idx_change > 0.03:
        reason = "国债指数偏强，利率债情绪较暖"
    elif idx_change < -0.03:
        reason = "国债指数回落，长债估值承压"
    elif abs(base) >= 0.015:
        reason = "自身净值趋势主导，国债指数小幅修正"
    else:
        reason = "票息收益为主，利率波动有限"

    confidence = "中" if r_squared >= 0.3 and quality in ("中", "强") else "低"
    return {
        "type": "rate_bond",
        "signal": signal,
        "tone": tone,
        "range_low": low,
        "range_high": high,
        "center": center,
        "reason": reason,
        "confidence": confidence,
        "benchmark": "利率债/国债曲线",
        "source": "自身净值趋势+国债曲线代理",
        "beta": round(float(beta or 0.0), 3),
        "r_squared": round(float(r_squared or 0.0), 3),
    }


def _weighted_average_quote(items: list[tuple[Optional["IndexInfo"], float]]) -> tuple[Optional[float], Optional[float]]:
    total_weight = 0.0
    weighted_change = 0.0
    weighted_current = 0.0
    for item, weight in items:
        if item is None or weight <= 0:
            continue
        try:
            current = float(getattr(item, "current", 0.0) or 0.0)
            change = float(getattr(item, "daily_change", 0.0) or 0.0)
        except Exception:
            continue
        if current <= 0:
            continue
        total_weight += weight
        weighted_change += change * weight
        weighted_current += current * weight
    if total_weight <= 0:
        return None, None
    return round(weighted_current / total_weight, 2), round(weighted_change / total_weight, 3)


async def fetch_bond_market_proxy_index() -> "IndexInfo":
    """债基估算专用债券代理篮子。

    020741 是利率债/中短债属性更强的债券基金。单看一个国债指数容易太钝，
    这里用国内可访问行情拼一个轻量代理：国债指数为主，辅以企债指数和
    场内债券 ETF。前台仍显示为“债券利率”，不把它当成股票指数。
    """
    specs = [
        ("sh000012", "国债指数", 0.48),
        ("sh000013", "企债指数", 0.22),
        ("sh511010", "国债ETF", 0.18),
        ("sh511260", "十年国债ETF", 0.12),
    ]
    results = await asyncio.gather(
        *(fetch_generic_index(code, name) for code, name, _weight in specs),
        return_exceptions=True
    )
    weighted_items: list[tuple[Optional[IndexInfo], float]] = []
    main: Optional[IndexInfo] = None
    for (code, _name, weight), result in zip(specs, results):
        item = result if isinstance(result, IndexInfo) else None
        if code == "sh000012" and item and item.current > 0:
            main = item
        weighted_items.append((item, weight))

    current, change = _weighted_average_quote(weighted_items)
    if main is None:
        main = next((item for item, _weight in weighted_items if item and item.current > 0), None)
    if main is None:
        return IndexInfo(code="bond_proxy", name="债券利率", current=0.0, previous=0.0, daily_change=0.0, history=[])

    display_current = float(getattr(main, "current", 0.0) or 0.0)
    display_change = change if change is not None else float(getattr(main, "daily_change", 0.0) or 0.0)
    previous = display_current / (1 + display_change / 100) if display_current > 0 and display_change != -100 else float(getattr(main, "previous", 0.0) or 0.0)
    return IndexInfo(
        code="bond_proxy",
        name="债券利率",
        current=round(display_current, 2),
        previous=round(previous, 2),
        daily_change=round(display_change, 3),
        history=[]
    )


async def compute_fund_specific_model(fund_code: str, daily_change: float, nav_date: str):
    """统一的基金专用模型计算入口
    1. index_following: 实时指数 + 残差修正
    2. multi_factor: 多指数加权 + 残差修正
    3. residual_only: 返回 (None, None, "") 由调用方自己处理 gsz 残差

    返回: (model_estimated_change, model_benchmark_change, model_benchmark_name)
          任何字段返回 None 表示该字段未计算
    """
    import asyncio as _aio
    model_config = FUND_SPECIFIC_MODELS.get(fund_code, {})
    model_type = model_config.get("type", "residual_only")

    if model_type == "residual_only":
        return None, None, ""

    if model_type == "index_following":
        idx_code = model_config.get("benchmark_code", "")
        bench_name = model_config.get("benchmark_name", "")
        idx_data = await fetch_realtime_index(idx_code)
        if not idx_data:
            return None, None, bench_name
        bench_change = idx_data.get("change_pct", 0.0)
        # 20:00+ 实际净值已出，记录指数残差（仅当基准变化够大）
        if daily_change != 0 and datetime.now().hour >= 20 and abs(bench_change) > 0.01:
            record_index_residual(fund_code, bench_change, daily_change, nav_date)
        model_stats = get_index_model_estimate(fund_code)
        if model_stats["enabled"]:
            est = round(bench_change + model_stats["offset"], 3)
        else:
            est = bench_change
        return est, bench_change, bench_name

    if model_type == "multi_factor":
        # 稳定多因子：默认权重兜底；回归结果质量足够时，只做部分融合，避免权重漂移。
        factors = model_config.get("factors", [])
        default_weights = {f["code"]: float(f.get("default_weight", 0.0) or 0.0) for f in factors}
        weights = dict(default_weights)
        alpha = 0.0
        fit = MULTI_FACTOR_REGRESSION.get(fund_code, {}) or {}
        fit_weights = fit.get("weights") or {}
        fit_ok = (
            fit_weights
            and int(fit.get("sample_days", 0) or 0) >= 10
            and float(fit.get("r_squared", 0.0) or 0.0) >= 0.25
        )
        if fit_ok:
            positive = {}
            for code, default_w in default_weights.items():
                try:
                    raw_w = float(fit_weights.get(code, 0.0) or 0.0)
                except Exception:
                    raw_w = 0.0
                if raw_w > 0:
                    positive[code] = min(raw_w, max(default_w * 2.2, 0.18))
            pos_sum = sum(positive.values())
            default_sum = sum(default_weights.values()) or 1.0
            if pos_sum > 0:
                normalized = {code: positive.get(code, 0.0) / pos_sum * default_sum for code in default_weights}
                weights = {
                    code: round(default_weights.get(code, 0.0) * 0.55 + normalized.get(code, 0.0) * 0.45, 4)
                    for code in default_weights
                }
                try:
                    alpha = max(-0.12, min(0.12, float(fit.get("alpha", 0.0) or 0.0)))
                except Exception:
                    alpha = 0.0

        async def _fetch_factor_index(factor: dict):
            rt = await fetch_eastmoney_realtime_index(factor["code"], factor.get("name", ""))
            return rt or await fetch_realtime_index(factor["code"])

        # 并发拉取所有因子指数（避免串行等待），东方财富优先，腾讯兜底。
        idx_results = await _aio.gather(*[_fetch_factor_index(f) for f in factors])
        weighted_change = 0.0
        total_weight = 0.0
        names = []
        for f, idx in zip(factors, idx_results):
            code = f["code"]
            w = weights.get(code, 0.0)
            if idx and w > 0:
                weighted_change += w * idx.get("change_pct", 0.0)
                total_weight += w
                names.append(f.get("name", ""))
        if total_weight == 0:
            return None, None, ""
        bench_change = round(weighted_change / total_weight + alpha, 3)
        bench_name = "+".join(names)
        # 20:00+ 记录指数残差
        if daily_change != 0 and datetime.now().hour >= 20:
            record_index_residual(fund_code, bench_change, daily_change, nav_date)
        model_stats = get_index_model_estimate(fund_code)
        if model_stats["enabled"]:
            offset = max(-0.25, min(0.25, float(model_stats["offset"] or 0.0)))
            est = round(bench_change + offset, 3)
        else:
            est = bench_change
        return est, bench_change, bench_name

    if model_type == "bond_baseline":
        # 纯债估算兜底：天天基金 gsz 对债基经常为 0.00% 或长时间不动。
        # 这里改为基金自身净值趋势为主：近3日反映短期变化，近10日反映票息/中期均值。
        # 国债指数 beta 会在后续债基分支里只做小权重修正，避免单靠国债导致日收益失真。
        win = model_config.get("baseline_window", 5)
        short_win = model_config.get("short_window", 3)
        recent_hist = fetch_fund_history_sync(fund_code, days=win + 6)
        recent_changes = [h.get("daily_change") for h in recent_hist
                          if h.get("daily_change") is not None and abs(float(h.get("daily_change") or 0.0)) <= 0.8][-win:]
        if len(recent_changes) < 3:
            # 数据不足时退化为残差修正
            model_stats = get_index_model_estimate(fund_code)
            cstats = get_correction_stats(fund_code)
            offset = cstats.get("mean_residual", 0.0) if cstats.get("sample_count", 0) >= 3 else 0.0
            return round(offset, 3), 0.0, "gsz残差"
        short_changes = recent_changes[-short_win:]
        short_avg = sum(short_changes) / len(short_changes)
        long_avg = sum(recent_changes) / len(recent_changes)
        last_change = recent_changes[-1]
        baseline = round(short_avg * 0.55 + long_avg * 0.35 + last_change * 0.10, 4)
        # 叠加 gsz 残差（如果有 ≥3 样本）
        cstats = get_correction_stats(fund_code)
        residual_offset = 0.0
        if cstats.get("sample_count", 0) >= 3:
            # 债基 gsz 样本噪声较大，残差只轻微修正。
            residual_offset = cstats.get("mean_residual", 0.0) * 0.35
        est = round(baseline + residual_offset, 3)
        est = max(-0.18, min(0.18, est))
        # 用最近 N 天的标准差作为"预测区间"参考（用于 UI 显示）
        std = round((sum((x - baseline) ** 2 for x in recent_changes) / max(1, len(recent_changes) - 1)) ** 0.5, 4)
        return est, baseline, f"债基趋势 {short_win}/{win}日 σ{std:.4f}%"

    return None, None, ""


INDEX_RESIDUAL_CACHE = load_index_residual_cache()


# ============= 多因子回归（每日 20:00 后自学习） =============

def load_multi_factor() -> dict:
    if not os.path.exists(MULTI_FACTOR_FILE):
        return {}
    try:
        with open(MULTI_FACTOR_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_multi_factor(data: dict):
    try:
        with open(MULTI_FACTOR_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fit_multi_factor_regression(fund_code: str, days: int = 20) -> dict:
    """用最近 N 天的 [基金实际涨跌] vs [各指数实际涨跌] 拟合线性回归
    返回: {alpha, weights: {code: w}, r_squared, sample_days, last_update}
    """
    model = FUND_SPECIFIC_MODELS.get(fund_code, {})
    if model.get("type") != "multi_factor":
        return {}
    factors = model.get("factors", [])
    if not factors:
        return {}
    try:
        # 异步历史已经在外面 fetch 过，这里用同步辅助函数重新拉一次
        from datetime import timedelta
        fund_hist = fetch_fund_history_sync(fund_code, days=days + 5)
        if not fund_hist or len(fund_hist) < 10:
            return {}
        # fund_hist: [{date, close, daily_change}]，按日期升序
        # 收集每天的 (fund_change, {idx_code: idx_change})
        rows = []
        for h in fund_hist:
            d = h.get("date", "")
            fc = h.get("daily_change")
            if fc is None:
                continue
            idx_changes = {}
            ok = True
            for f in factors:
                idx_code = f["code"]
                idx_hist = fetch_index_history_sync(idx_code, days=days + 5)
                idx_row = next((x for x in idx_hist if x.get("date") == d), None)
                if not idx_row or idx_row.get("change") is None:
                    ok = False
                    break
                idx_changes[idx_code] = idx_row["change"]
            if ok:
                rows.append({"date": d, "fund": fc, "idx": idx_changes})
        if len(rows) < 10:
            return {}
        # 最小二乘：fund = alpha + sum(beta_i * idx_i)
        # 用闭式解
        codes = [f["code"] for f in factors]
        n = len(rows)
        k = len(codes)
        # 设计矩阵 X: [1, idx_1, idx_2, ...]; y: fund
        import numpy as np
        X = np.array([[1.0] + [r["idx"][c] for c in codes] for r in rows])
        y = np.array([r["fund"] for r in rows])
        # 正规方程 beta = (X'X)^-1 X'y
        try:
            beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        except Exception:
            return {}
        alpha = float(beta[0])
        weights = {codes[i]: round(float(beta[i + 1]), 4) for i in range(k)}
        # R²
        y_pred = X @ beta
        ss_res = float(np.sum((y - y_pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        result = {
            "alpha": round(alpha, 4),
            "weights": weights,
            "r_squared": round(r2, 4),
            "sample_days": n,
            "last_update": datetime.now().strftime("%Y-%m-%d")
        }
        MULTI_FACTOR_REGRESSION[fund_code] = result
        save_multi_factor(MULTI_FACTOR_REGRESSION)
        print(f"[多因子回归] {fund_code} 拟合完成: alpha={alpha:.4f}, weights={weights}, R²={r2:.4f}, n={n}")
        return result
    except ImportError:
        # numpy 不可用 → 用纯 Python 实现
        return _fit_multi_factor_pure_python(fund_code, factors, days)
    except Exception as e:
        print(f"[多因子回归] {fund_code} 失败: {e}")
        return {}


def _fit_multi_factor_pure_python(fund_code: str, factors: list, days: int) -> dict:
    """无 numpy 时的纯 Python OLS 拟合"""
    fund_hist = fetch_fund_history_sync(fund_code, days=days + 5)
    if not fund_hist or len(fund_hist) < 10:
        return {}
    rows = []
    for h in fund_hist:
        d = h.get("date", "")
        fc = h.get("daily_change")
        if fc is None:
            continue
        idx_changes = {}
        ok = True
        for f in factors:
            idx_code = f["code"]
            idx_hist = fetch_index_history_sync(idx_code, days=days + 5)
            idx_row = next((x for x in idx_hist if x.get("date") == d), None)
            if not idx_row or idx_row.get("change") is None:
                ok = False
                break
            idx_changes[idx_code] = idx_row["change"]
        if ok:
            rows.append({"date": d, "fund": fc, "idx": idx_changes})
    if len(rows) < 10:
        return {}
    codes = [f["code"] for f in factors]
    n = len(rows)
    k = len(codes)
    # 用高斯消元法解正规方程 X'X β = X'y
    # 构造 X'X (k+1)x(k+1) 和 X'y (k+1,)
    XtX = [[0.0] * (k + 1) for _ in range(k + 1)]
    Xty = [0.0] * (k + 1)
    for r in rows:
        x = [1.0] + [r["idx"][c] for c in codes]
        y = r["fund"]
        for i in range(k + 1):
            Xty[i] += x[i] * y
            for j in range(k + 1):
                XtX[i][j] += x[i] * x[j]
    # 高斯消元
    def solve(A, b):
        n = len(b)
        for i in range(n):
            # 选主元
            mx = abs(A[i][i])
            piv = i
            for r in range(i + 1, n):
                if abs(A[r][i]) > mx:
                    mx = abs(A[r][i])
                    piv = r
            A[i], A[piv] = A[piv], A[i]
            b[i], b[piv] = b[piv], b[i]
            if abs(A[i][i]) < 1e-12:
                return None
            for r in range(i + 1, n):
                f = A[r][i] / A[i][i]
                for c in range(i, n):
                    A[r][c] -= f * A[i][c]
                b[r] -= f * b[i]
        # 回代
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            x[i] = (b[i] - sum(A[i][j] * x[j] for j in range(i + 1, n))) / A[i][i]
        return x
    beta = solve([row[:] for row in XtX], Xty[:])
    if not beta:
        return {}
    alpha = beta[0]
    weights = {codes[i]: round(beta[i + 1], 4) for i in range(k)}
    # R²
    y_mean = sum(r["fund"] for r in rows) / n
    ss_res = 0.0
    ss_tot = 0.0
    for r in rows:
        y = r["fund"]
        y_pred = alpha + sum(weights[c] * r["idx"][c] for c in codes)
        ss_res += (y - y_pred) ** 2
        ss_tot += (y - y_mean) ** 2
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    result = {
        "alpha": round(alpha, 4),
        "weights": weights,
        "r_squared": round(r2, 4),
        "sample_days": n,
        "last_update": datetime.now().strftime("%Y-%m-%d")
    }
    MULTI_FACTOR_REGRESSION[fund_code] = result
    save_multi_factor(MULTI_FACTOR_REGRESSION)
    print(f"[多因子回归-PP] {fund_code} 拟合完成: alpha={alpha:.4f}, weights={weights}, R²={r2:.4f}, n={n}")
    return result


# 同步辅助函数（多因子回归需要）—— 复用 fetch_fund_history 的接口但用同步 HTTP
def fetch_fund_history_sync(fund_code: str, days: int = 30) -> list:
    """同步拉取基金历史净值（用于多因子回归）"""
    import requests as _req
    url = f"http://api.fund.eastmoney.com/f10/lsjz"
    params = {
        "fundCode": fund_code, "pageIndex": 1, "pageSize": days,
        "mode": "1", "_": int(datetime.now().timestamp() * 1000)
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"http://fundf10.eastmoney.com/jjjz_{fund_code}.html"
    }
    try:
        r = _req.get(url, params=params, headers=headers, timeout=8)
        data = r.json()
    except Exception:
        return []
    rows = []
    prev_close = None
    for item in reversed(data.get("Data", {}).get("LSJZList", [])):
        try:
            nav = float(item.get("DWJZ") or item.get("NAV") or 0)
            if nav <= 0:
                continue
            d = item.get("FSRQ", "")[:10]
            daily_change = None
            if prev_close is not None and prev_close > 0:
                daily_change = round((nav - prev_close) / prev_close * 100, 3)
            rows.append({"date": d, "close": nav, "daily_change": daily_change})
            prev_close = nav
        except Exception:
            continue
    return rows


def fetch_index_history_sync(index_code: str, days: int = 30) -> list:
    """同步拉取指数历史（日线收盘 + 当日涨跌幅）
    使用腾讯 K 线接口（与 fetch_realtime_index 同源，更稳定）
    返回: [{date, close, change}, ...] 升序
    """
    import requests as _req
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={index_code},day,,,{days + 5},qfq&r=0.1"
    try:
        r = _req.get(url, headers=HTTP_HEADERS, timeout=8)
        r.raise_for_status()
    except Exception as e:
        print(f"[指数历史] {index_code} 拉取失败: {e}")
        return []
    text = r.text
    # 复用 _parse_tencent_kline
    raw = _parse_tencent_kline(text, index_code)
    if not raw:
        return []
    # raw: [(date, close, open), ...] 升序
    rows = []
    for i, item in enumerate(raw):
        if len(item) < 2:
            continue
        d, close = item[0], item[1]
        if i == 0:
            change = None
        else:
            prev_close = raw[i - 1][1]
            change = round((close - prev_close) / prev_close * 100, 3) if prev_close > 0 else None
        rows.append({"date": d, "close": close, "change": change})
    return rows


MULTI_FACTOR_REGRESSION = load_multi_factor()


def load_correction_cache() -> dict:
    """加载残差修正缓存
    结构:
    {
      "_meta": {"last_update": "2026-07-10", "max_samples": 20},
      "011609": {
        "samples": [
          {"date": "2026-07-09", "est_change": -0.42, "actual_change": -0.31, "residual": 0.11}
        ],
        "mean_residual": 0.05,
        "std_residual": 0.03,
        "sample_count": 8,
        "confidence": "high"   # high(>=5) / medium(>=3) / low(<3) / none
      }
    }
    """
    if not os.path.exists(CORRECTION_CACHE_FILE):
        return {"_meta": {"last_update": "", "max_samples": 20}}
    try:
        with open(CORRECTION_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"_meta": {"last_update": "", "max_samples": 20}}


def save_correction_cache(data: dict):
    """保存残差修正缓存"""
    try:
        data.setdefault("_meta", {})["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(CORRECTION_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_correction_stats(fund_code: str) -> dict:
    """获取某只基金的残差修正统计"""
    return CORRECTION_CACHE.get(fund_code, {})


def get_last_est_change(fund_code: str) -> float:
    """从 correction_cache 中取最近一次（非今天）的 est_change
    用于未开盘时显示"上一交易日预估"参考
    """
    entry = CORRECTION_CACHE.get(fund_code, {})
    samples = entry.get("samples", []) or []
    if not samples:
        return 0.0
    today = datetime.now().strftime("%Y-%m-%d")
    # 按 (date, time) 倒序：取最大 date，date 相同时取最大 time
    def _key(s):
        return (s.get("date", ""), s.get("time", ""))
    sorted_samples = sorted(samples, key=_key, reverse=True)
    for s in sorted_samples:
        d = s.get("date", "")
        if d and d != today:
            try:
                return round(float(s.get("est_change", 0)), 2)
            except (ValueError, TypeError):
                return 0.0
    return 0.0


def record_residual(fund_code: str, est_change: float, actual_change: float, trade_date: str, sample_time: str = ""):
    """记录一条残差样本
    residual = actual - est，正值表示天天基金低估了跌幅/高估了涨幅
    est_change 因 gsz 接口精度会四舍五入到 0；actual_change 为 0 才是真无数据
    sample_time: 快照时点（如 "10:05"），用于区分同一天多个采样点
    020741 (纯债) 特殊：gsz 经常返回 0.00%，但 actual 可能 0.03%+
    → 只要 actual 足够大（≥0.02%）就接受，即使 est 接近 0
    """
    if actual_change == 0:
        return
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    # 纯债基金：actual 足够大就接受（gsz 四舍五入为 0 实际可能 0.03%+）
    if fund_code == "020741":
        if abs(actual_change) < 0.02 and abs(est_change) < 0.005:
            return  # 两者都极小，无意义
    else:
        if abs(est_change) < 0.005:
            return  # 股票基金严格要求 est 有意义

    fund_entry = CORRECTION_CACHE.setdefault(fund_code, {"samples": []})
    samples = fund_entry.setdefault("samples", [])

    # 去重：同一天+同时点只保留一条（允许多时点样本共存）
    def _same(s):
        return s.get("date") == trade_date and (s.get("time") or "") == (sample_time or "")

    samples = [s for s in samples if not _same(s)]
    samples.append({
        "date": trade_date,
        "time": sample_time,  # 快照时点
        "est_change": round(est_change, 3),
        "actual_change": round(actual_change, 3),
        "residual": round(actual_change - est_change, 3)
    })
    # 只保留最近 60 个样本（多时点+多日）
    max_samples = CORRECTION_CACHE.get("_meta", {}).get("max_samples", 60)
    samples = samples[-max_samples:]
    fund_entry["samples"] = samples

    # 计算均值/标准差/置信度
    residuals = [s["residual"] for s in samples]
    n = len(residuals)
    mean_r = round(sum(residuals) / n, 4)
    if n >= 2:
        var_r = sum((x - mean_r) ** 2 for x in residuals) / (n - 1)
        std_r = round(var_r ** 0.5, 4)
    else:
        std_r = 0.0

    if n >= 5:
        confidence = "high"
    elif n >= 3:
        confidence = "medium"
    elif n >= 1:
        confidence = "low"
    else:
        confidence = "none"

    fund_entry["mean_residual"] = mean_r
    fund_entry["std_residual"] = std_r
    fund_entry["sample_count"] = n
    fund_entry["confidence"] = confidence
    fund_entry["last_residual"] = residuals[-1]
    fund_entry["last_update"] = trade_date

    save_correction_cache(CORRECTION_CACHE)


def get_effective_correction(fund_code: str) -> dict:
    """获取某只基金当前可用的修正系数
    返回: {offset, std, confidence, sample_count, enabled}
    """
    stats = get_correction_stats(fund_code)
    n = stats.get("sample_count", 0)
    confidence = stats.get("confidence", "none")
    mean_r = stats.get("mean_residual", 0.0)
    std_r = stats.get("std_residual", 0.0)
    # 至少 3 个样本才启用修正，避免冷启动噪声
    enabled = n >= 3 and confidence in ("medium", "high")
    # 对债券基金更保守：要求 std 不能太大
    return {
        "offset": mean_r if enabled else 0.0,
        "std": std_r,
        "confidence": confidence,
        "sample_count": n,
        "enabled": enabled
    }


def has_today_residual(fund_code: str, trade_date: str) -> bool:
    """检查某只基金在指定交易日是否已记录过残差"""
    if not trade_date:
        return False
    stats = get_correction_stats(fund_code)
    for s in stats.get("samples", []):
        if s.get("date") == trade_date:
            return True
    return False


CORRECTION_CACHE = load_correction_cache()


# ============= 盘中估值快照（多时点） =============

def load_intraday_snapshots() -> dict:
    """加载盘中估值快照
    结构:
    {
      "_meta": {"max_snapshots_per_fund": 60, "snapshot_times": ["10:00","13:00","14:30"]},
      "011609": {
        "samples": [
          {"date": "2026-07-10", "time": "10:05", "est_change": -0.85, "actual_change": -5.25, "residual": -4.40},
          {"date": "2026-07-10", "time": "13:05", "est_change": -3.10, "actual_change": -5.25, "residual": -2.15},
          {"date": "2026-07-10", "time": "14:32", "est_change": -5.20, "actual_change": -5.25, "residual": -0.05}
        ]
      }
    }
    """
    if not os.path.exists(INTRADAY_SNAPSHOTS_FILE):
        return {"_meta": {"max_snapshots_per_fund": 60, "snapshot_times": SNAPSHOT_TIMES}}
    try:
        with open(INTRADAY_SNAPSHOTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault("_meta", {})["snapshot_times"] = SNAPSHOT_TIMES
        return data
    except Exception:
        return {"_meta": {"max_snapshots_per_fund": 60, "snapshot_times": SNAPSHOT_TIMES}}


def save_intraday_snapshots(data: dict):
    try:
        with open(INTRADAY_SNAPSHOTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def has_today_snapshot(fund_code: str, trade_date: str, snapshot_time: str) -> bool:
    """检查某只基金在指定日期+时点是否已有快照"""
    if not trade_date or not snapshot_time:
        return False
    data = INTRADAY_SNAPSHOTS
    samples = data.get(fund_code, {}).get("samples", [])
    for s in samples:
        if s.get("date") == trade_date and s.get("time", "").startswith(snapshot_time[:5]):
            return True
    return False


async def take_intraday_snapshot(fund_code: str, snapshot_time: str):
    """对单只基金在指定时点采集 gsz 估值快照（后台任务调用）"""
    try:
        url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js?rt=1"
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers={**HTTP_HEADERS, "Referer": "https://fund.eastmoney.com/"})
            text = r.text
        m = re.search(r'jsonpgz\((.*)\)', text, re.S)
        if not m:
            return
        data = json.loads(m.group(1))
        gszzl = data.get('gszzl', '')
        if not gszzl or gszzl in ('null', '-'):
            return
        if abs(float(gszzl)) < 0.005:
            return  # 债券基金 gsz 精度限制，跳过
        est_change = round(float(gszzl), 3)
        trade_date = datetime.now().strftime("%Y-%m-%d")
        # 同一日期+时点去重
        fund_entry = INTRADAY_SNAPSHOTS.setdefault(fund_code, {"samples": []})
        samples = fund_entry.setdefault("samples", [])
        samples = [s for s in samples if not (s.get("date") == trade_date and s.get("time", "").startswith(snapshot_time[:5]))]
        samples.append({
            "date": trade_date,
            "time": snapshot_time,
            "est_change": est_change,
            "actual_change": 0.0,  # 收盘后补全
            "residual": 0.0
        })
        max_n = INTRADAY_SNAPSHOTS.get("_meta", {}).get("max_snapshots_per_fund", 60)
        samples = samples[-max_n:]
        fund_entry["samples"] = samples
        save_intraday_snapshots(INTRADAY_SNAPSHOTS)
        print(f"[快照] {fund_code} {trade_date} {snapshot_time} 估值={est_change:+.3f}%")
    except Exception as e:
        print(f"[快照] {fund_code} {snapshot_time} 失败: {e}")


def finalize_day_snapshots(trade_date: str, fund_data_lookup: dict):
    """对指定日期所有已采集的快照，用实际涨跌补全 residual 并写入 CORRECTION_CACHE
    fund_data_lookup: {fund_code: daily_change}
    """
    if not trade_date:
        return
    for fund_code, daily_change in fund_data_lookup.items():
        if daily_change == 0:
            continue
        fund_entry = INTRADAY_SNAPSHOTS.get(fund_code, {})
        samples = fund_entry.get("samples", [])
        updated = False
        for s in samples:
            if s.get("date") != trade_date:
                continue
            if s.get("actual_change", 0) == 0:
                s["actual_change"] = round(daily_change, 3)
                s["residual"] = round(daily_change - s.get("est_change", 0), 3)
                updated = True
        if updated:
            # 把已 finalize 的快照写一份到 CORRECTION_CACHE（每个时点一个样本）
            for s in samples:
                if s.get("date") == trade_date and s.get("residual", 0) != 0:
                    record_residual(fund_code, s["est_change"], s["actual_change"], trade_date, s.get("time", ""))
            save_intraday_snapshots(INTRADAY_SNAPSHOTS)


INTRADAY_SNAPSHOTS = load_intraday_snapshots()


def load_est_cache() -> dict:
    """从文件加载估算缓存，**不过期** —— 周末/节假日保留最后一个交易日的 gsz 估值
    用户要求"预估永远是预估"，周末保持前一个交易时间的预估
    """
    if not os.path.exists(EST_CACHE_FILE):
        return {}
    try:
        with open(EST_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except Exception:
        return {}


def save_est_cache(data: dict):
    """保存估算缓存到文件，带日期标记"""
    try:
        data["_date"] = datetime.now().strftime("%Y-%m-%d")
        with open(EST_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


EST_CACHE = load_est_cache()


# ============= 基金历史净值持久化 =============
# 每天只拉"最新 1 天"增量 append，避免每次强制刷新都重拉 30 天
# 文件结构：{ "fund_code": [{"date": "2026-07-20", "nav": 1.5, "acc_nav": 1.5, "change": 0.3}, ...] }
# 按日期升序，只保留最近 60 天（超过的自动裁掉）
NAV_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "nav_history.json")
NAV_HISTORY_KEEP_DAYS = 60  # 文件里保留 60 天，远超实际使用 30 天

def load_nav_history() -> dict:
    """从文件加载历史净值，按 fund_code 分组"""
    if not os.path.exists(NAV_HISTORY_FILE):
        return {}
    try:
        with open(NAV_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_nav_history(data: dict):
    """保存历史净值到文件（裁掉 60 天前的）"""
    try:
        cutoff = (datetime.now() - timedelta(days=NAV_HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
        for code in list(data.keys()):
            data[code] = [r for r in data[code] if r.get("date", "") >= cutoff]
            # 按日期升序
            data[code].sort(key=lambda x: x.get("date", ""))
        with open(NAV_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 nav_history 失败: {e}")

NAV_HISTORY = load_nav_history()


def get_cached_est(fund_code: str) -> dict:
    """获取缓存的估算数据，保留最后一次有效数据（当日有效）"""
    return EST_CACHE.get(fund_code, {})


def set_cached_est(fund_code: str, est_nav: float, est_change: float, est_time: str):
    """缓存估算数据（内存+文件）
    _est_date: 标记 fetch 的日期，用于盘末/跨日时区分"今天的 15:00 锁定值"和"昨天/前几天的旧值"
    """
    EST_CACHE[fund_code] = {
        "est_nav": est_nav,
        "est_change": est_change,
        "est_time": est_time,
        "_est_date": datetime.now().strftime("%Y-%m-%d")
    }
    save_est_cache(EST_CACHE)

# 获取web-app目录（当前目录的父目录）
web_app_dir = os.path.dirname(current_dir)
index_path = os.path.join(web_app_dir, "index.html")

# 买点配置文件路径（持久化）
buy_points_file = os.path.join(current_dir, "buy_points.json")

# 挂载静态文件目录
app.mount("/static", NoCacheStaticFiles(directory=current_dir), name="static")

# 允许跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============= 数据模型 =============

class NavHistoryItem(BaseModel):
    """单日净值历史条目"""
    date: str          # 净值日期，如 2026-06-18
    nav: float         # 单位净值
    change: float      # 日涨跌 %
    acc_nav: float = 0.0  # 累计净值


class BuyPointInfo(BaseModel):
    """买点信息 + 持有收益"""
    cost_nav: float           # 成本净值（买入价/虚拟成本）
    current_nav: float        # 当前净值
    yield_pct: float          # 当前收益率 %
    can_buy: bool             # 是否可以买（仅未持仓时有效）
    drop_pct: float           # 距买点的距离 %（正数表示距离买点还有多少）
    is_holding: bool = False  # 是否已经买入持有
    buy_date: str = ""        # 买入日期
    buy_price: float = 0.0    # 买入价格（等于cost_nav，冗余便于前端理解）
    hold_days: int = 0        # 持有天数
    total_return: float = 0.0  # 总收益率（相对买入价）%
    yield_history: List[dict] = []  # 买入以来每日收益率
    # === 买点进度与参考值 ===
    hist_yield: float = 0.0   # 历史基准收益率（从配置）
    buy_point_yield: float = 0.0  # 买点阈值收益率（hist_yield - 3%）
    target_nav: float = 0.0   # 买点对应的净值（即达到买点阈值时的净值）
    progress_pct: float = 0.0  # 买点进度百分比（0-100）
    drop_threshold: float = 0.0  # 买点下跌阈值 %
    ref_date: str = ""           # 买点判断开始日期
    ref_nav: float = 0.0         # 买点参考净值
    shares: float = 0.0        # 份额权重（用于补仓加权成本）
    realized_yield_pct: float = 0.0  # 最近一次卖出实现收益率 %
    transactions: List[dict] = []    # 买入/卖出流水


class AIPrediction(BaseModel):
    """AI预判信息"""
    trend: str           # 趋势：上涨/下跌/震荡/观望
    trend_emoji: str     # emoji图标
    advice: str         # 预判建议
    confidence: str      # 置信度：谨慎/中性/乐观
    # 当日评估
    est_nav: float = 0.0       # 估算净值（今日盘中）
    est_change: float = 0.0    # 估算日涨跌 %
    est_time: str = ""         # 估算时间
    est_vs_last: float = 0.0   # 估算净值相对昨日净值涨跌 %
    today_verdict: str = ""    # 当日判断：强势/偏弱/震荡等
    # ============ 专业详细分析（前端折叠展示） ============
    market_env: str = ""       # 市场环境分析（大盘/债市）
    tech_analysis: str = ""     # 技术面分析（近N日走势、均线、极值）
    position_advice: str = ""   # 仓位/操作建议（已持仓/未持仓分别给出）
    risk_tips: str = ""        # 风险提示
    key_metrics: str = ""      # 关键指标摘要（例如"7日波动率 / 最大回撤 / 连涨连跌天数"）
    # ============ 增强分析 ============
    valuation_position: str = ""  # 估值位置：当前净值在历史区间位置（百分位）
    cost_performance: str = ""     # 性价比评估：低估/合理/高估
    key_levels: str = ""          # 关键点位：支撑位、压力位
    risk_level: str = ""          # 风险等级：低/中/高
    # ============ 阶段性收益率 ============
    return_7d: float = 0.0         # 近7日收益率
    return_1m: float = 0.0         # 近1月收益率
    return_6m: float = 0.0         # 近半年收益率


class FundInfo(BaseModel):
    """基金信息模型"""
    code: str
    name: str
    type: str
    current_nav: float
    previous_nav: float
    nav_date: str
    daily_change: float
    estimated: bool = False
    estimated_nav: float = 0.0    # 今日估算净值
    estimated_change: float = 0.0  # 估算日涨跌 %
    estimated_time: str = ""       # 估算时间
    history: List[NavHistoryItem] = []  # 历史净值（近130天，支持半年收益计算）
    buy_point: BuyPointInfo = None      # 买点信息（含收益率）
    ai_prediction: AIPrediction = None  # AI预判（含当日评估）
    buy_point_ref_date: str = ""        # 买点起始日期
    holdings: List[dict] = []           # 基金持仓（重仓股）
    prev_est_change: float = 0.0        # 上一交易日预估涨跌（%），未开盘时显示用
    # === 残差修正模型（自建估值） ===
    corrected_estimated_change: float = 0.0   # 修正后的盘中估值涨跌 %
    correction_offset: float = 0.0            # 当前应用的修正系数（%）
    correction_std: float = 0.0               # 残差标准差（%）
    correction_confidence: str = "none"       # none/low/medium/high
    correction_sample_count: int = 0          # 历史样本数
    # === 基金专用模型（指数跟随 / 多因子） ===
    model_type: str = "residual_only"         # index_following / multi_factor / residual_only
    model_estimated_change: float = 0.0       # 模型估值（更准）
    model_benchmark_change: float = 0.0       # 基准指数实时涨跌 %
    model_benchmark_name: str = ""            # 基准名称（科创50 / 上证50）
    model_offset: float = 0.0                 # 基金-基准的均残差
    model_std: float = 0.0
    model_confidence: str = "none"
    model_sample_count: int = 0
    model_enabled: bool = False               # 样本 ≥ 3 时启用
    bond_estimate: Dict[str, object] = {}      # 债基专属估算：方向、区间、原因、置信度
    # === 阶段性收益率（顶层汇总，从天天基金手机 API 抓取） ===
    return_7d: float = 0.0       # 近7日累计收益率（%）
    return_1m: float = 0.0       # 近1月累计收益率（%）
    return_3m: float = 0.0       # 近3月累计收益率（%）
    return_6m: float = 0.0       # 近6月累计收益率（%）


class NewsItem(BaseModel):
    """市场资讯条目模型"""
    title: str
    url: str
    time: str = ""
    fetched_at: str = ""
    sentiment: str = "neutral"   # bullish | bearish | neutral
    tags: List[str] = []         # 事件标签，如 ["央行", "降准"]
    event_date: str = ""         # 明确的未来事件日期，如 2026-07-29
    importance: int = 0          # 事件重要性评分，前端可用于重点日期排序


# 情绪判别关键词
BULLISH_KEYWORDS = ["利好", "上涨", "突破", "创新高", "增长", "提速", "扩张", "超预期",
                    "提振", "复苏", "企稳", "强势", "看涨", "走强", "放量", "回暖", "走高",
                    "降准", "降息", "减税", "补贴", "扶持", "盈利", "业绩超", "盈利预增",
                    "连涨", "收涨", "收高", "大涨", "飙升", "涨停", "拉升", "反弹",
                    "新高", "增长超", "扭转", "盈利改善", "预喜", "中标", "签约", "回购股份",
                    "增持", "回购", "分红", "派息", "获批", "提速", "扩张", "提速", "走升"]
BEARISH_KEYWORDS = ["下跌", "下挫", "破位", "创新低", "下滑", "萎缩", "低于预期", "承压",
                    "疲软", "看跌", "走弱", "缩量", "走低", "加息", "收紧", "去杠杆", "调控",
                    "亏损", "减产", "风险", "违规", "处罚", "暴跌", "跳水", "跌停",
                    "连跌", "收跌", "收低", "大跌", "重挫", "破发", "腰斩", "预减", "预亏",
                    "诉讼", "起诉", "窃取", "侵权", "造假", "欺诈", "退市", "停牌", "ST",
                    "诉讼", "调查", "处罚", "通报", "违规", "暴跌", "下挫", "重挫", "跳水",
                    "危机", "动荡", "恐慌", "避险", "走弱", "走跌", "跌", "亏", "挫",
                    "制裁", "限制", "禁令", "关税", "摩擦", "冲突", "瘫痪", "中断"]
IMPORTANT_STOCK_KEYWORDS = ["A股", "上证", "沪指", "深成指", "创业板", "科创", "科创50", "沪深300", "中证", "股票", "股市", "两市", "板块", "半导体", "芯片", "科技", "AI", "人工智能", "新能源", "券商", "银行", "央行", "降准", "降息", "LPR", "利率", "国债", "债券", "资金面", "北向", "成交额", "基金", "美股", "港股", "纳指", "道指", "标普", "外围", "欧洲央行", "美联储", "逆回购", "DR001", "MLF", "通胀", "CPI", "PPI", "关税", "贸易", "原油", "石油", "黄金", "美元", "汇率", "人民币", "存储", "内存", "晶圆", "光通信", "三星", "博通", "英伟达", "特斯拉", "苹果", "微软", "伊朗", "中东", "地缘"]
OPINION_NEWS_KEYWORDS = ["观点", "评论", "点评", "解读", "研报", "策略", "建议", "认为", "预计", "预期", "看好", "看空", "提示", "提醒", "机构", "券商", "分析师", "首席", "专家", "投资者", "市场人士", "人士称", "表示", "称", "或", "有望", "午评", "收评", "盘前", "盘后", "复盘", "前瞻", "展望", "怎么看", "如何看", "值得关注", "值得期待", "配置", "掘金", "机会", "风险提示"]
OPINION_SOURCE_KEYWORDS = ["机构", "券商", "分析师", "首席", "专家", "市场人士", "投资者", "私募", "公募", "基金经理", "经济学家", "交易员"]
OPINION_ACTION_KEYWORDS = ["认为", "预计", "预期", "表示", "称", "建议", "看好", "看空", "提示", "提醒", "判断", "解读"]
REAL_EVENT_KEYWORDS = ["公告", "发布", "获批", "签署", "启动", "完成", "通过", "落地", "召开", "举行", "上市", "停牌", "复牌", "涨停", "跌停", "大涨", "大跌", "收涨", "收跌", "开盘", "收盘", "成交", "上调", "下调", "增持", "减持", "回购", "分红", "发行", "处罚", "调查", "起诉", "突发", "刚刚"]
HARD_EVENT_KEYWORDS = ["公告", "获批", "签署", "启动", "完成", "通过", "落地", "召开", "举行", "上市", "停牌", "复牌", "涨停", "跌停", "大涨", "大跌", "收涨", "收跌", "开盘", "收盘", "成交额", "上调", "下调", "增持", "减持", "回购", "分红", "发行", "处罚", "调查", "起诉", "突发", "刚刚", "降准", "降息", "逆回购", "MLF", "LPR", "拿下", "达成", "供应", "通报", "宣布"]

EVENT_TAGS = {
    "央行": ["央行", "人民银行", "货币政策", "降准", "降息", "MLF", "逆回购", "LPR", "公开市场", "PBOC", "美联储", "Fed"],
    "政策": ["政策", "改革", "规划", "意见", "方案", "指导", "通知", "文件", "国务院", "证监会", "银保监"],
    "财报": ["业绩", "盈利", "营收", "利润", "季报", "年报", "中报", "财报", "净利润", "营收增长", "预增", "预减", "预亏", "预喜"],
    "行业": ["行业", "板块", "产业链", "赛道", "概念股", "新能源", "半导体", "芯片", "光伏", "锂电", "汽车", "医药"],
    "汇率": ["汇率", "人民币", "美元", "外汇", "离岸", "在岸", "日元", "欧元", "美元指数", "G10"],
    "突发": ["紧急", "突发", "重大", "刚刚", "突发!", "突发:", "诉讼", "起诉", "调查", "制裁"],
    "利率": ["利率", "回购", "票据", "国债", "债券", "美债", "美债收益率", "10年期", "10Y"],
    "地缘": ["制裁", "战争", "冲突", "海峡", "霍尔木兹", "台海", "俄乌", "中东", "伊朗"],
    "AI科技": ["AI", "人工智能", "OpenAI", "ChatGPT", "芯片", "半导体", "英伟达", "黄仁勋", "Meta", "微软", "谷歌", "Apple", "苹果"],
    "海外": ["美股", "港股", "纳指", "道指", "标普", "外围", "欧洲央行", "美联储"],
    "商品": ["原油", "石油", "黄金", "有色", "铜", "大宗商品", "化工品"],
    "流动性": ["逆回购", "DR001", "MLF", "资金面", "票据"],
}


def news_importance_score(title: str) -> int:
    """重点股市资讯评分：A股/指数/科创/债券/政策/基金相关优先。"""
    score = sum(1 for kw in IMPORTANT_STOCK_KEYWORDS if kw in title)
    if any(kw in title for kw in ["重大", "突发", "刚刚", "收评", "午评", "盘中", "开盘", "收盘"]):
        score += 1
    if any(kw in title for kw in ["娱乐", "体育", "彩票", "房产", "旅游"]):
        score -= 3
    return score


def is_opinion_news(title: str) -> bool:
    """7x24 中很多是观点/评论，只作为辅助信息，不挤占主资讯列表。"""
    title = re.sub(r"\s+", "", title or "")
    if any(src in title for src in OPINION_SOURCE_KEYWORDS) and any(act in title for act in OPINION_ACTION_KEYWORDS):
        return True
    if any(kw in title for kw in OPINION_NEWS_KEYWORDS):
        return True
    return False


def is_hard_event_news(title: str) -> bool:
    """能进主列表的应是客观事件、政策动作、市场异动或公司公告。"""
    title = re.sub(r"\s+", "", title or "")
    if is_opinion_news(title):
        return False
    return any(kw in title for kw in HARD_EVENT_KEYWORDS)


def classify_news(title: str) -> tuple:
    """对单条新闻做情绪 + 事件标签判别
    返回: (sentiment, tags)
    """
    # 情绪判别：先看利空（避免"业绩超预期"被错误判为"业绩"+利好，"业绩低于预期"应该被判为利空）
    sentiment = "neutral"
    bearish_hits = sum(1 for kw in BEARISH_KEYWORDS if kw in title)
    bullish_hits = sum(1 for kw in BULLISH_KEYWORDS if kw in title)
    if bearish_hits > bullish_hits:
        sentiment = "bearish"
    elif bullish_hits > bearish_hits:
        sentiment = "bullish"
    # 事件标签
    tags = []
    for tag, kws in EVENT_TAGS.items():
        if any(kw in title for kw in kws):
            tags.append(tag)
            if len(tags) >= 3:
                break
    return sentiment, tags


class IndexHistoryItem(BaseModel):
    """指数单日历史"""
    date: str
    close: float
    change: float


class IndexInfo(BaseModel):
    """指数信息模型"""
    code: str
    name: str
    current: float
    previous: float
    daily_change: float
    history: List[IndexHistoryItem] = []  # 近7天历史


class ThemeSectorInfo(BaseModel):
    """主题板块温度（按持仓主题/指数/政策资讯聚合）"""
    name: str
    value: Optional[float] = None
    label: str = ""
    note: str = ""
    tone: str = "neutral"  # good | bad | neutral
    source: str = ""
    updated_at: str = ""
    history: List[IndexHistoryItem] = []
    return_7d: Optional[float] = None
    return_1m: Optional[float] = None
    return_6m: Optional[float] = None
    detail: str = ""
    detail_type: str = "index"


THEME_MARKET_INDEXES = [
    ("医疗", "sz399989", "中证医疗"),
    ("白酒", "sz399997", "中证白酒"),
    ("新能源", "sz399808", "中证新能源"),
    ("消费", "sh000932", "中证消费"),
    ("券商", "sz399975", "证券公司"),
    ("银行", "sz399986", "中证银行"),
]


class PortfolioResponse(BaseModel):
    """持仓响应模型"""
    date: str
    time: str
    is_trading_day: bool = False
    display_trade_date: str = ""
    latest_disclosed_date: str = ""
    market_status: str = "closed"
    index: IndexInfo
    funds: List[FundInfo]
    news: List[NewsItem]
    bond_index: Optional[IndexInfo] = None  # 债券指数（用于债券基金参考）
    k50_index: Optional[IndexInfo] = None  # 科创50指数（首页市场卡片）
    hsi_index: Optional[IndexInfo] = None  # 恒生指数（首页市场卡片）
    hs300_index: Optional[IndexInfo] = None  # 沪深300指数
    sz_index: Optional[IndexInfo] = None  # 深证成指
    theme_sectors: List[ThemeSectorInfo] = []  # 主题板块温度
    external_markets: List[ThemeSectorInfo] = []  # 外部市场温度（美股/韩国）
    historical_yields: Dict[str, dict] = {}  # 历史收益基准（含日期）


# ============= 配置数据 =============

# 用户关注的基金列表
WATCHED_FUNDS = [
    "011609",  # 易方达上证科创50联接C
    "020741",  # 华泰保兴安悦债券C
    "004746",  # 易方达上证50增强C
]
CORE_FUNDS = set(WATCHED_FUNDS)

# HTTP 客户端配置
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
}

# 历史收益率配置（成本基准）
# 结构: { fund_code: { "yield": 收益率, "date": 基准日期 } }
HISTORICAL_YIELDS = {
    "011609": {"yield": 0.0, "date": "2026-06-18"},
    "020741": {"yield": 2.04, "date": "2026-06-18"},
    "004746": {"yield": 28.12, "date": "2026-06-18"},
}
# 收益率下降阈值（%）：收益率比历史基准下降3个百分点触发买点
DROP_THRESHOLD = 3.0

# ============= /api/portfolio 整页缓存 =============
# 30s 内重复请求直接复用旧数据，避免重复刷新打爆 eastmoney 接口
# ?force=1 绕过缓存（强制刷新按钮用）
PORTFOLIO_CACHE: dict = {"data": None, "saved_at": 0.0, "disclosed": False}
PORTFOLIO_CACHE_TTL = 30  # 秒（盘中 30s）
PORTFOLIO_CACHE_TTL_OFFHOURS = 3600  # 秒（非盘中兜底 1h，但实际用"到下一交易日 9:30"的动态 TTL）
BACKGROUND_PORTFOLIO_REFRESHING = False
BACKGROUND_PORTFOLIO_STATUS = {
    "last_started_at": "",
    "last_finished_at": "",
    "last_error": "",
    "last_duration_seconds": None,
    "next_interval_seconds": None,
}


def portfolio_snapshot_max_age_seconds() -> int:
    return 120 if is_trading_time() else 300


def schedule_portfolio_refresh(reason: str = "stale_snapshot"):
    global BACKGROUND_PORTFOLIO_REFRESHING
    if BACKGROUND_PORTFOLIO_REFRESHING:
        return
    BACKGROUND_PORTFOLIO_REFRESHING = True

    async def _refresh_once():
        global BACKGROUND_PORTFOLIO_REFRESHING
        try:
            await get_portfolio(force=1, lite=0)
            print(f"[后台刷新] {reason} 已刷新 {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[后台刷新] {reason} 失败: {e}")
        finally:
            BACKGROUND_PORTFOLIO_REFRESHING = False

    try:
        asyncio.create_task(_refresh_once())
    except RuntimeError:
        BACKGROUND_PORTFOLIO_REFRESHING = False
        pass


def _latest_disclosed_date() -> str:
    """
    当前最新可披露日期：
    - 工作日 15:00 后（disclosure window 之后）→ 今天
    - 其他时间（盘中 / 周末 / 节假日）→ 上一个交易日
    """
    now = datetime.now()
    if now.weekday() < 5 and now.hour >= 15:
        return now.strftime("%Y-%m-%d")
    # 倒推到上一个工作日
    n = now - timedelta(days=1)
    while n.weekday() >= 5:
        n -= timedelta(days=1)
    return n.strftime("%Y-%m-%d")


def _display_trade_date(now: Optional[datetime] = None) -> str:
    """
    基金卡片显示日期：
    - 显示最新已披露净值日期，避免次日/盘前把昨天实际收益误判成今天预估
    """
    return _latest_disclosed_date()


def _is_today_disclosed() -> bool:
    """
    判断 cache 中的 nav_date 是否覆盖"最新可披露日期"
    - 盘中（9:30-15:00）：日涨跌未披露，强制 False（继续拉盘中估值）
    - 盘外（15:00 后 / 9:30 前 / 周末 / 节假日）：
        - 所有基金 nav_date == 最新可披露日期（today 或上一交易日）→ True
    """
    if PORTFOLIO_CACHE["data"] is None:
        return False
    # 盘中：日涨跌未定，强制 False
    if is_trading_time():
        return False
    # 盘外
    funds = getattr(PORTFOLIO_CACHE["data"], "funds", []) or []
    if not funds:
        return False
    latest = _latest_disclosed_date()
    return all(f.nav_date == latest for f in funds)


def _next_market_open_ts(now: datetime) -> float:
    """
    下一个交易日 9:30 的 timestamp
    - 工作日 9:30 之前：今天 9:30
    - 工作日 9:30 之后 / 周末：下一个工作日 9:30
    """
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < candidate:
        # 今天 9:30 还没到，且今天是工作日
        if now.weekday() < 5:
            return candidate.timestamp()
    # 否则跳到下一个工作日 9:30
    nxt = now + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()

# ============= 基金子接口细粒度缓存 =============
# period_returns / history 这两个接口返回的数据盘中几乎不变，60s 内重复请求直接复用
# force=1 强制刷新时绕过这两个子缓存（但 fetch_fund_holdings 当日缓存不受影响）
# 内存缓存，不落盘（重启失效，避免 5 只基金每次都重新拉 130 天 history 浪费时间）
FUND_DETAIL_CACHE: dict = {}  # {fund_code: {"period_returns": ..., "history": ..., "_saved_at": ts}}
FUND_DETAIL_CACHE_TTL = 60  # 秒

# ============= 新闻 10 分钟缓存 =============
# 资讯列表每 10 分钟重新抓取一次，避免同一时段长期不更新
NEWS_LIST_CACHE_TTL = 600
NEWS_CACHE: dict = {"data": None, "saved_at": 0.0}
NEWS_REFRESHING = False
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def get_deepseek_api_key() -> str:
    candidates = [os.getenv("DEEPSEEK_API_KEY", "").strip()]
    for path in [
        os.path.join(BASE_DIR, "deepseek.key"),
        os.path.join(os.path.dirname(BASE_DIR), "deepseek.key"),
        "/opt/fund-manager/deepseek.key",
    ]:
        try:
            if os.path.exists(path):
                candidates.append(open(path, "r", encoding="utf-8").read().strip())
        except Exception:
            pass
    for raw in candidates:
        if not raw:
            continue
        match = re.search(r"sk-[A-Za-z0-9_-]+", raw)
        if match:
            return match.group(0)
        cleaned = raw.encode("ascii", "ignore").decode("ascii").strip()
        if cleaned.startswith("sk-"):
            return cleaned
    return ""


def fallback_news_items() -> List[NewsItem]:
    now_str = datetime.now().strftime("%m-%d %H:%M")
    return [NewsItem(
        title="资讯后台更新中，稍后自动刷新",
        url="https://finance.sina.com.cn/",
        time=now_str,
        fetched_at=now_str,
        tags=["后台更新"],
        source="系统"
    )]


def current_news_or_placeholder() -> List[NewsItem]:
    return NEWS_CACHE["data"] or fallback_news_items()


async def refresh_news_background(force: bool = False) -> None:
    global NEWS_REFRESHING
    if NEWS_REFRESHING:
        return
    NEWS_REFRESHING = True
    try:
        await fetch_market_news(force=force)
    except Exception as e:
        print(f"后台刷新资讯失败: {e}")
    finally:
        NEWS_REFRESHING = False


def schedule_news_refresh(force: bool = False) -> None:
    if NEWS_REFRESHING:
        return
    asyncio.create_task(refresh_news_background(force=force))

def _get_news_bucket(now: datetime) -> str:
    """根据当前时间返回新闻时段 bucket：AM / PM / NIGHT"""
    h = now.hour
    if 6 <= h < 12:
        return "AM"
    elif 12 <= h < 18:
        return "PM"
    else:
        return "NIGHT"

# ============= 买点计算规则（新） =============
# 按基金独立配置：从下一个交易日（最新净值）开始，下跌多少百分点触发买点
# 结构: { fund_code: { "drop_threshold": 5.0 } }
BUY_POINT_CONFIG = {
    "011609": {"drop_threshold": 10.0},   # 下跌10个点
    "020741": {"drop_threshold": 1.0},    # 下跌1个点
    "004746": {"drop_threshold": 5.0},    # 下跌5个点
}

fund_settings_file = os.path.join(current_dir, "fund_settings.json")


def load_fund_settings() -> Dict[str, dict]:
    try:
        if os.path.exists(fund_settings_file):
            with open(fund_settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"加载基金关注配置失败: {e}")
    return {}


def save_fund_settings(settings: Dict[str, dict]) -> bool:
    try:
        with open(fund_settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存基金关注配置失败: {e}")
        return False


FUND_SETTINGS: Dict[str, dict] = load_fund_settings()


def apply_fund_settings() -> None:
    for code, cfg in FUND_SETTINGS.items():
        code = str(code).strip()
        if not code:
            continue
        if code not in WATCHED_FUNDS:
            WATCHED_FUNDS.append(code)
        HISTORICAL_YIELDS[code] = {
            "yield": float(cfg.get("historical_yield", 0.0) or 0.0),
            "date": str(cfg.get("historical_date") or cfg.get("follow_date") or datetime.now().strftime("%Y-%m-%d"))
        }
        BUY_POINT_CONFIG[code] = {
            "drop_threshold": float(cfg.get("drop_threshold", 5.0) or 5.0)
        }


apply_fund_settings()

# 买点参考价缓存 —— 持久化每只基金的"下一个交易日起点净值"
# 结构: { fund_code: { "ref_nav": 1.4507, "ref_date": "2026-06-21" } }
# 规则：一旦保存，ref_nav 就固定不变，作为买点计算的起点
buy_point_refs_file = os.path.join(current_dir, "buy_point_refs.json")
BUY_POINT_REFS: Dict[str, dict] = {}


def load_buy_point_refs() -> Dict[str, dict]:
    """从本地文件加载买点参考价（仅含 ref_nav/ref_date）"""
    try:
        if os.path.exists(buy_point_refs_file):
            with open(buy_point_refs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {k: {"ref_nav": float(v.get("ref_nav", 0)),
                                "ref_date": str(v.get("ref_date", ""))}
                            for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        print(f"加载买点参考价失败: {e}")
    return {}


def save_buy_point_refs(refs: Dict[str, dict]) -> bool:
    """保存买点参考价到本地文件"""
    try:
        with open(buy_point_refs_file, "w", encoding="utf-8") as f:
            json.dump(refs, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存买点参考价失败: {e}")
        return False


BUY_POINT_REFS = load_buy_point_refs()

# 成本净值缓存 — 支持用户"确定买入"
COST_NAVS: Dict[str, dict] = {}


def load_cost_navs_from_file() -> Dict[str, dict]:
    """从本地文件加载成本净值 —— 只保留用户真正确认买入的记录（is_holding=True）
    其他未确认的虚拟成本一律忽略，保证 buy_points.json 是用户操作的纯净记录。
    """
    try:
        if os.path.exists(buy_points_file):
            with open(buy_points_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and len(data) > 0:
                    result = {}
                    for k, v in data.items():
                        # 兼容老格式: {code: float}。旧版本里这个值可能就是用户持仓成本，
                        # 不能直接忽略，否则重启后持仓收益会全部变成 0 / 未持仓。
                        if isinstance(v, (int, float)):
                            result[k] = {
                                "buy_nav": float(v),
                                "buy_date": "",
                                "buy_price": float(v),
                                "shares": 1.0,
                                "realized_yield_pct": 0.0,
                                "yield_pct": 0.0,
                                "total_return": 0.0,
                                "transactions": [],
                                "is_holding": True,
                                "sell_date": "",
                                "sell_price": 0.0
                            }
                            continue
                        # 新格式: {code: {buy_nav, buy_date, is_holding}} —— 保留用户真实交易记录
                        # is_holding=False 的已卖出记录也要保留，否则重启后已实现收益会从历史累计收益中丢失。
                        elif isinstance(v, dict):
                            shares = float(v.get("shares", 1.0 if v.get("is_holding", False) else 0.0) or 0.0)
                            is_holding = bool(
                                v.get("is_holding", False)
                                or v.get("holding", False)
                                or (shares > 0 and not v.get("sell_date"))
                            )
                            stored_yield = float(
                                v.get("yield_pct", v.get("total_return", v.get("holding_yield_pct", 0.0))) or 0.0
                            )
                            result[k] = {
                                "buy_nav": float(v.get("buy_nav", v.get("cost_nav", v.get("buy_price", 0))) or 0),
                                "buy_date": str(v.get("buy_date", "")),
                                "buy_price": float(v.get("buy_price", v.get("buy_nav", 0)) or 0),
                                "shares": shares if shares > 0 else (1.0 if is_holding else 0.0),
                                "realized_yield_pct": float(v.get("realized_yield_pct", 0.0) or 0.0),
                                "yield_pct": stored_yield,
                                "total_return": stored_yield,
                                "transactions": v.get("transactions", []),
                                "is_holding": is_holding,
                                "sell_date": str(v.get("sell_date", "")),
                                "sell_price": float(v.get("sell_price", 0.0) or 0.0)
                            }
                    return result
    except Exception as e:
        print(f"加载成本净值失败: {e}")
    return {}


def save_cost_navs_to_file(cost_navs: Dict[str, dict]) -> bool:
    """保存成本净值到本地文件"""
    try:
        with open(buy_points_file, "w", encoding="utf-8") as f:
            json.dump(cost_navs, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存成本净值失败: {e}")
        return False


def compute_cost_navs_from_historical(current_navs: Dict[str, float]) -> Dict[str, float]:
    """根据当前净值和历史收益率计算成本净值"""
    result = {}
    for code, hist_data in HISTORICAL_YIELDS.items():
        hist_yield = hist_data["yield"] if isinstance(hist_data, dict) else hist_data
        cur_nav = current_navs.get(code)
        if cur_nav and cur_nav > 0:
            result[code] = round(cur_nav / (1 + hist_yield / 100), 4)
    return result


COST_NAVS = load_cost_navs_from_file()


def ensure_added_fund_tracking_baseline(fund_code: str, current_nav: float, nav_date: str) -> None:
    """add-fund-tracking-baseline-20260729: 新增关注基金自动补齐买点监控和历史累计基准。"""
    fund_code = str(fund_code or "").strip()
    if not fund_code or fund_code not in FUND_SETTINGS:
        return
    try:
        nav_value = round(float(current_nav or 0.0), 4)
    except Exception:
        nav_value = 0.0
    if nav_value <= 0:
        return

    cfg = FUND_SETTINGS.get(fund_code, {}) if isinstance(FUND_SETTINGS.get(fund_code), dict) else {}
    follow_date = str(cfg.get("follow_date") or cfg.get("historical_date") or nav_date or datetime.now().strftime("%Y-%m-%d"))[:10]
    ref_date = str(nav_date or follow_date)[:10] or follow_date
    historical_yield = round(float(cfg.get("historical_yield", 0.0) or 0.0), 2)
    changed_refs = False
    changed_costs = False

    ref_data = BUY_POINT_REFS.get(fund_code, {}) if isinstance(BUY_POINT_REFS.get(fund_code), dict) else {}
    try:
        ref_nav = round(float(ref_data.get("ref_nav", 0.0) or 0.0), 4)
    except Exception:
        ref_nav = 0.0
    if ref_nav <= 0:
        BUY_POINT_REFS[fund_code] = {"ref_nav": nav_value, "ref_date": ref_date}
        ref_nav = nav_value
        changed_refs = True

    cost_data = COST_NAVS.get(fund_code, {}) if isinstance(COST_NAVS.get(fund_code), dict) else {}
    try:
        cost_nav = round(float(cost_data.get("buy_nav", 0.0) or cost_data.get("cost_nav", 0.0) or 0.0), 4)
    except Exception:
        cost_nav = 0.0
    if cost_nav <= 0:
        COST_NAVS[fund_code] = {
            "buy_nav": ref_nav,
            "buy_date": follow_date,
            "buy_price": ref_nav,
            "shares": 0.0,
            "realized_yield_pct": 0.0,
            "yield_pct": historical_yield,
            "total_return": historical_yield,
            "transactions": [],
            "is_holding": False,
            "sell_date": "",
            "sell_price": 0.0,
        }
        changed_costs = True

    if changed_refs:
        save_buy_point_refs(BUY_POINT_REFS)
    if changed_costs:
        save_cost_navs_to_file(COST_NAVS)
        save_cost_navs_to_db(COST_NAVS)


# ============= 数据抓取函数 =============

async def fetch_fund_period_returns(fund_code: str, force: int = 0) -> dict:
    """
    从天天基金手机 API 获取区间收益率（近1周/1月/3月/6月/1年/2年/3年/5年/今年/成立以来）
    返回 {"Z": 0.07, "Y": 0.69, "3Y": 1.85, ...}（百分比，无数据时为 None）
    force=1 强制刷新（绕过 60s 子缓存）
    """
    # === 60s 子缓存：盘中区间收益基本不变 ===
    if not force:
        cached = FUND_DETAIL_CACHE.get(fund_code, {})
        if cached.get("period_returns") is not None and (time.time() - cached.get("_saved_at", 0)) < FUND_DETAIL_CACHE_TTL:
            return cached["period_returns"]
    url = (
        f"https://fundmobapi.eastmoney.com/FundMNewApi/FundMNPeriodIncrease"
        f"?FCODE={fund_code}&deviceid=W&plat=Wap&product=EFund&version=2.0.0"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://fund.eastmoney.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        result = {}
        for item in (data.get("Datas") or []):
            title = item.get("title", "")
            syl = item.get("syl", "")
            try:
                result[title] = float(syl) if syl not in (None, "", "--") else None
            except (ValueError, TypeError):
                result[title] = None
        # === 写入子缓存 ===
        if fund_code not in FUND_DETAIL_CACHE:
            FUND_DETAIL_CACHE[fund_code] = {}
        FUND_DETAIL_CACHE[fund_code]["period_returns"] = result
        FUND_DETAIL_CACHE[fund_code]["_saved_at"] = time.time()
        return result
    except Exception as e:
        print(f"获取基金 {fund_code} 区间收益失败: {e}")
        return {}


async def fetch_fund_history(fund_code: str, days: int = 7, force: int = 0) -> List[NavHistoryItem]:
    """
    从天天基金网获取基金历史净值数据（最近N天）
    持久化策略：nav_history.json 存最近 60 天，force=1 也只拉"最新 1 天"增量 append
    注意：API单页上限约40条，需多页获取
    关键：必须传 sdate/edate，否则端点返回空 content
    force=1 强制刷新（绕过 60s 子缓存，但仍走持久化增量拉取）
    """
    # === 60s 子缓存：盘中历史净值基本不变（按 days 维度缓存） ===
    cache_key_days = f"history_{days}"
    if not force:
        cached = FUND_DETAIL_CACHE.get(fund_code, {})
        if cached.get(cache_key_days) is not None and (time.time() - cached.get("_saved_at", 0)) < FUND_DETAIL_CACHE_TTL:
            return cached[cache_key_days]

    # === 持久化增量：算 last_date，决定只拉哪段 ===
    persisted = NAV_HISTORY.get(fund_code, [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    if persisted:
        last_date = persisted[-1].get("date", "")
        # 如果 last_date 已经是今天或更晚 → 直接返回持久化
        if last_date >= today_str:
            history = [NavHistoryItem(**r) for r in persisted[-days:]]
            # 写子缓存
            if fund_code not in FUND_DETAIL_CACHE:
                FUND_DETAIL_CACHE[fund_code] = {}
            FUND_DETAIL_CACHE[fund_code][cache_key_days] = history
            FUND_DETAIL_CACHE[fund_code]["_saved_at"] = time.time()
            return history
        # 否则只拉 last_date+1 到 today
        sdate = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        edate = today_str
    else:
        # 首次/为空 → 拉 days 天
        sdate = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
        edate = today_str

    per_page = 40
    # 计算需要几页
    days_to_fetch = (datetime.strptime(edate, "%Y-%m-%d") - datetime.strptime(sdate, "%Y-%m-%d")).days + 1
    total_pages = max(1, (days_to_fetch + per_page - 1) // per_page)

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": f"http://fund.eastmoney.com/{fund_code}.html",
    }

    all_rows = []
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            for page in range(1, total_pages + 1):
                url = f"http://fund.eastmoney.com/F10/F10DataApi.aspx?type=lsjz&code={fund_code}&page={page}&per={per_page}&sdate={sdate}&edate={edate}&rt=json"
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                text = response.text

                content_match = re.search(r'content:"([^"]+)"', text)
                if not content_match:
                    break

                html_content = content_match.group(1)
                rows = re.findall(r"<tr><td>(\d{4}-\d{2}-\d{2})</td><td[^>]*>([\d.]+)</td><td[^>]*>([\d.]+)</td><td[^>]*>([-\d.]+)%</td>", html_content)

                if not rows:
                    break
                all_rows.extend(rows)
                if len(all_rows) >= days_to_fetch:
                    break

        # === 合并：持久化历史 + 新拉数据（去重按 date） ===
        existing_dates = {r.get("date", "") for r in persisted}
        new_records = []
        for date_str, nav_str, acc_nav_str, change_str in all_rows:
            if date_str in existing_dates:
                continue
            new_records.append({
                "date": date_str,
                "nav": float(nav_str),
                "acc_nav": float(acc_nav_str),
                "change": float(change_str),
            })
        if new_records:
            persisted.extend(new_records)
            persisted.sort(key=lambda x: x["date"])
            NAV_HISTORY[fund_code] = persisted
            save_nav_history(NAV_HISTORY)

        # 取最近 days 条
        recent = persisted[-days:] if len(persisted) >= days else persisted
        history = [NavHistoryItem(**r) for r in recent]

        # === 写入子缓存 ===
        if fund_code not in FUND_DETAIL_CACHE:
            FUND_DETAIL_CACHE[fund_code] = {}
        FUND_DETAIL_CACHE[fund_code][cache_key_days] = history
        FUND_DETAIL_CACHE[fund_code]["_saved_at"] = time.time()
        return history

    except Exception as e:
        print(f"获取基金 {fund_code} 历史数据失败: {e}")
        # 失败时回退到持久化数据（如果有）
        if persisted:
            return [NavHistoryItem(**r) for r in persisted[-days:]]
        return []


async def fetch_fund_holdings(fund_code: str) -> List[dict]:
    """
    从天天基金网获取基金持仓（重仓股）
    返回前十大持仓股列表，每项包含股票代码、名称、持仓比例
    加当日缓存：持仓数据每天只变一次（季报披露日才更新），没必要每次刷新都拉
    """
    # === 当日缓存：同一天内直接复用 ===
    today_str = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"_holdings_{fund_code}"
    cached = EST_CACHE.get(cache_key, {})
    if cached.get("_date") == today_str and cached.get("data") is not None:
        return cached["data"]

    url = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={fund_code}&topline=10"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://fundf10.eastmoney.com/ccmx_{fund_code}.html",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            text = response.text

        # 解析返回的HTML内容
        # 格式: var apidata={ content:"<table>...</table>", ...}
        content_match = re.search(r'content:"([^"]+)"', text)
        if not content_match:
            return []

        html_content = content_match.group(1)
        # 解析持仓表格：股票代码、名称、持仓比例、占净值比例
        # 格式: <td>股票代码</td><td>股票名称</td><td>持仓比例</td><td>占净值比例</td>
        rows = re.findall(r"<td[^>]*>(\d+)</td><td[^>]*>([^<]+)</td><td[^>]*>([\d.]+)%</td><td[^>]*>([\d.]+)%</td>", html_content)

        holdings = []
        for code, name, proportion, net_ratio in rows[:10]:
            holdings.append({
                "code": code,
                "name": name.strip(),
                "proportion": float(proportion),  # 持仓比例%
                "net_ratio": float(net_ratio)     # 占净值比例%
            })

        # === 写入当日缓存（季报披露日才更新，没必要每次都拉） ===
        EST_CACHE[cache_key] = {"_date": today_str, "data": holdings}
        save_est_cache(EST_CACHE)

        return holdings

    except Exception as e:
        print(f"获取基金 {fund_code} 持仓数据失败: {e}")
        return []


def generate_ai_prediction_simple(fund_type: str, fund_name: str, daily_change: float,
                                  history: List[NavHistoryItem],
                                  cost_nav: float, current_nav: float,
                                  est_nav: float, est_change: float,
                                  market_index: Optional[IndexInfo] = None,
                                  bond_extra: Optional[dict] = None,
                                  period_returns: Optional[dict] = None) -> AIPrediction:
    """
    根据基金类型、近期走势、估算净值 + 市场指数生成AI预判和当日评估
    （不依赖用户持仓成本，纯市场/技术面分析）
    market_index: 该基金对应的市场指数（股票/指数型=上证指数，债券型=国债指数）
    """
    fund_type_lower = (fund_type + fund_name).lower()
    is_bond = any(k in fund_type_lower for k in ['债券', '债', '稳健', '纯债', '二级债'])
    is_index = any(k in fund_type_lower for k in ['指数', 'etf', '联接', '增强'])
    is_stock = any(k in fund_type_lower for k in ['股票', '混合', '灵活', '成长', '价值'])

    # ============ 阶段性收益率计算（7日/1月/6月）============
    def calc_period_returns() -> tuple:
        """根据 history 计算 7日、1月、6月收益率（百分比）
        history 按日期倒序排列（最新在前）
        优先使用 period_returns（天天基金手机 API），无数据时用 history 兜底
        """
        # 手机 API 字段映射：Z=近7日, Y=近1月, 3Y=近3月, 6Y=近6月
        if period_returns:
            r7 = period_returns.get("Z")
            r1m = period_returns.get("Y")
            r6m = period_returns.get("6Y")
            # 三个值都拿到了就直接返回
            if r7 is not None and r1m is not None and r6m is not None:
                return (round(r7, 2), round(r1m, 2), round(r6m, 2))

        if not history or len(history) < 2 or current_nav <= 0:
            return (0.0, 0.0, 0.0)

        def period_return(offset_days: int) -> float:
            if len(history) <= offset_days:
                offset_days = len(history) - 1
            old_nav = history[offset_days].nav
            if old_nav > 0:
                return round((current_nav - old_nav) / old_nav * 100, 2)
            return 0.0

        r7 = period_return(7)    # 近7日（约一周交易日）
        r1m = period_return(22)  # 近1月（约22个交易日）
        r6m = period_return(130) # 近6月（约130个交易日）
        return (r7, r1m, r6m)

    return_7d, return_1m, return_6m = calc_period_returns()

    # ============ 增强分析：估值位置、性价比、关键点位、风险等级 ============
    def calc_enhanced_analysis() -> tuple:
        """计算增强分析：返回(估值位置, 性价比评估, 关键点位, 风险等级)"""
        if not history or len(history) < 5:
            return ("数据不足", "数据不足", "支撑/压力位待观察", "中")

        navs = [h.nav for h in history]
        min_nav = min(navs)
        max_nav = max(navs)
        current = current_nav if current_nav > 0 else (navs[0] if navs else 0)

        # 1. 估值位置：当前净值在历史区间的百分位
        if max_nav > min_nav:
            pct = (current - min_nav) / (max_nav - min_nav) * 100
            if pct <= 20:
                val_pos = f"历史低位({pct:.0f}%)"
                cost_perf = "低估"
            elif pct <= 50:
                val_pos = f"偏低位({pct:.0f}%)"
                cost_perf = "偏低估"
            elif pct <= 80:
                val_pos = f"偏高位({pct:.0f}%)"
                cost_perf = "合理"
            else:
                val_pos = f"历史高位({pct:.0f}%)"
                cost_perf = "高估"
        else:
            val_pos = "估值不明"
            cost_perf = "合理"

        # 2. 关键点位：支撑位(近5日低点)、压力位(近5日高点)
        recent_5 = navs[:5]
        support = min(recent_5)
        resistance = max(recent_5)
        key_levels = f"支撑{support:.4f} / 压力{resistance:.4f}"

        # 3. 风险等级：根据近期波动率和涨跌幅判断
        changes = [h.change for h in history[:5]]
        volatility = sum(abs(c) for c in changes) / len(changes) if changes else 0
        recent_total = sum(changes) if changes else 0

        if is_bond:
            if volatility > 0.1:
                risk = "中"
            else:
                risk = "低"
        else:
            if volatility > 3.0 or abs(recent_total) > 8:
                risk = "高"
            elif volatility > 1.5 or abs(recent_total) > 4:
                risk = "中"
            else:
                risk = "低"

        return (val_pos, cost_perf, key_levels, risk)

    valuation_position, cost_performance, key_levels, risk_level = calc_enhanced_analysis()

    # 分析近期走势
    recent_changes = [h.change for h in history[:5]] if history else []
    if recent_changes:
        total_change = sum(recent_changes)
        avg_change = total_change / len(recent_changes) if recent_changes else 0
        consecutive_up = 0
        consecutive_down = 0
        for c in recent_changes:
            if c > 0:
                consecutive_up += 1
                consecutive_down = 0
            elif c < 0:
                consecutive_down += 1
                consecutive_up = 0
            else:
                break
    else:
        total_change = 0.0
        avg_change = 0.0
        consecutive_up = 0
        consecutive_down = 0

    # 市场指数参考文字
    market_ref = ""
    if market_index and market_index.current > 0 and abs(market_index.daily_change) > 0.001:
        idx_change = market_index.daily_change
        if is_bond:
            if abs(idx_change) < 0.05:
                market_ref = "，债市今日整体平稳"
            elif idx_change > 0:
                market_ref = "，债市今日偏暖"
            else:
                market_ref = "，债市今日小幅调整"
        else:
            if abs(idx_change) < 0.3:
                market_ref = "，大盘今日震荡整理"
            elif idx_change > 0:
                market_ref = f"，大盘今日偏强（上证指数+{idx_change:.2f}%）"
            else:
                market_ref = f"，大盘今日偏弱（上证指数{idx_change:.2f}%）"

    # 当日判断：综合使用估算涨跌
    today_change = est_change if est_change != 0 else daily_change
    today_verdict = ""
    if is_index:
        if today_change > 1.0:
            today_verdict = "强势领涨"
        elif today_change > 0.3:
            today_verdict = "偏强"
        elif today_change < -1.0:
            today_verdict = "明显回调"
        elif today_change < -0.3:
            today_verdict = "偏弱"
        else:
            today_verdict = "震荡整理"
    elif is_bond:
        if abs(today_change) < 0.03:
            today_verdict = "平稳"
        elif today_change > 0:
            today_verdict = "小幅上行"
        else:
            today_verdict = "小幅调整"
    else:
        if today_change > 1.5:
            today_verdict = "大幅走强"
        elif today_change > 0.5:
            today_verdict = "偏强"
        elif today_change < -1.5:
            today_verdict = "明显回调"
        elif today_change < -0.5:
            today_verdict = "偏弱"
        else:
            today_verdict = "震荡整理"

    # === 债券型 ===
    if is_bond:
        # 债券基金详细分析
        bond_beta = bond_extra.get("beta", 0.5) if bond_extra else 0.5
        bond_r2 = bond_extra.get("r_squared", 0) if bond_extra else 0
        corr_quality = bond_extra.get("correlation_quality", "弱相关") if bond_extra else "弱相关"

        # 市场环境分析
        if market_index and market_index.current > 0:
            idx_chg = market_index.daily_change
            if abs(idx_chg) < 0.02:
                market_env = "债市今日整体平稳，利率波动极小，资金面保持均衡状态"
            elif idx_chg > 0:
                market_env = f"债市今日偏暖（国债指数+{idx_chg:.2f}%），利率小幅下行，资金面相对宽松"
            else:
                market_env = f"债市今日小幅调整（国债指数{idx_chg:.2f}%），利率略有上行，关注资金面变化"
        else:
            market_env = "债市数据待更新，整体以票息收益为主"

        # 技术面分析
        if len(history) >= 10:
            navs_10 = [h.nav for h in history[:10]]
            navs_30 = [h.nav for h in history[:30]] if len(history) >= 30 else navs_10
            ma5 = sum(navs_10[:5]) / 5
            ma10 = sum(navs_10) / 10
            ma30 = sum(navs_30) / len(navs_30)
            if current_nav > ma5 > ma10:
                tech = f"短期均线多头排列（5日/10日均线向上），净值稳步抬升，近5日均值{ma5:.4f}，近10日均值{ma10:.4f}，趋势偏强"
            elif current_nav < ma5 < ma10:
                tech = f"短期均线空头排列（5日/10日均线向下），净值有所回调，近5日均值{ma5:.4f}，近10日均值{ma10:.4f}，短期偏弱"
            else:
                tech = f"净值在均线附近震荡，5日均值{ma5:.4f}，10日均值{ma10:.4f}，30日均值{ma30:.4f}，方向不明朗"
            # 波动率
            changes_20 = [h.change for h in history[:20]] if len(history) >= 20 else [h.change for h in history]
            vol = sum(abs(c) for c in changes_20) / len(changes_20) if changes_20 else 0
            tech += f"；近20日日均波动{vol:.3f}%"
        else:
            tech = "历史数据不足，技术分析参考有限"

        # 操作建议
        if today_change > 0.03 and return_7d > 0:
            position_advice = "已持仓者可继续持有，享受票息+资本利得；未持仓者可小额定投，避免追高"
        elif today_change < -0.03:
            position_advice = "短期回调无需过度担忧，债券基金以持有到期思路为主，可逢低分批加仓"
        else:
            position_advice = "平稳运行阶段，以持有为主，定投可继续按计划进行，关注利率走势变化"

        # 风险提示
        risk_tips = "主要风险来自利率上行、信用违约和流动性收紧；中长期看债市仍有配置价值"
        key_metrics = f"7日收益{return_7d:+.2f}% / 1月收益{return_1m:+.2f}% / 半年收益{return_6m:+.2f}% / 利率敏感度β={bond_beta:.2f}（{corr_quality}）"

        if today_change > 0.05:
            return AIPrediction(
                trend="震荡偏强", trend_emoji="📊",
                advice=f"债券基金今日偏暖，票息收益为主{market_ref}，利率趋势值得关注",
                confidence="中性",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                market_env=market_env,
                tech_analysis=tech,
                position_advice=position_advice,
                risk_tips=risk_tips,
                key_metrics=key_metrics,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change < -0.05:
            return AIPrediction(
                trend="小幅回调", trend_emoji="📉",
                advice=f"债基今日小幅调整{market_ref}，短期波动但长端利率走势平稳，信用利差无明显走扩",
                confidence="乐观",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                market_env=market_env,
                tech_analysis=tech,
                position_advice=position_advice,
                risk_tips=risk_tips,
                key_metrics=key_metrics,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        else:
            return AIPrediction(
                trend="平稳", trend_emoji="➡️",
                advice=f"债券基金今日平稳运行{market_ref}，票息收益为主，利率波动有限",
                confidence="乐观",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                market_env=market_env,
                tech_analysis=tech,
                position_advice=position_advice,
                risk_tips=risk_tips,
                key_metrics=key_metrics,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )

    # === 指数型 ===
    if is_index:
        if today_change > 1.5:
            return AIPrediction(
                trend="强势上涨", trend_emoji="🚀",
                advice=f"指数基金今日大幅上涨(+{today_change:.2f}%){market_ref}，短期动能较强，但需警惕冲高回落",
                confidence="谨慎",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change > 0.3:
            return AIPrediction(
                trend="跟随上涨", trend_emoji="⬆️",
                advice=f"跟随大盘上涨{market_ref}，短期动能尚可，注意上方压力位和板块轮动",
                confidence="中性",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change < -1.5:
            return AIPrediction(
                trend="大幅回调", trend_emoji="⚠️",
                advice=f"指数基金大幅下跌({today_change:.2f}%){market_ref}，市场情绪趋弱，需关注下方支撑和成交量变化",
                confidence="谨慎",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change < -0.3:
            if consecutive_down >= 3:
                return AIPrediction(
                    trend="持续走弱", trend_emoji="🔻",
                    advice=f"已连续{consecutive_down}天下跌{market_ref}，趋势偏弱，短期超跌后可能有技术性反弹",
                    confidence="谨慎",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
            else:
                return AIPrediction(
                    trend="震荡回调", trend_emoji="📉",
                    advice=f"大盘走弱拖累指数{market_ref}，短期以观望为主，等待企稳信号",
                    confidence="中性",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
        else:
            if consecutive_up >= 3:
                return AIPrediction(
                    trend="连续走强", trend_emoji="📈",
                    advice=f"已连续{consecutive_up}日上涨{market_ref}，短期趋势偏强，注意上方压力和回调风险",
                    confidence="谨慎",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
            else:
                return AIPrediction(
                    trend="震荡整理", trend_emoji="➡️",
                    advice=f"指数处于震荡整理阶段{market_ref}，方向不明，等待动能释放或趋势明朗",
                    confidence="中性",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )

    # === 股票/混合型 ===
    if is_stock or not is_bond:
        if today_change > 2.0:
            return AIPrediction(
                trend="大幅走强", trend_emoji="🔥",
                advice=f"基金今日大幅上涨({today_change:.2f}%){market_ref}，表现强势，但短期涨幅较大，注意动能持续性",
                confidence="谨慎",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change > 0.5:
            if total_change > 3:
                return AIPrediction(
                    trend="趋势向好", trend_emoji="📈",
                    advice=f"近5日累计上涨{total_change:.2f}%{market_ref}，上升趋势良好，注意板块轮动和成交量变化",
                    confidence="乐观",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
            else:
                return AIPrediction(
                    trend="小幅上涨", trend_emoji="⬆️",
                    advice=f"随市上涨{market_ref}，短期动能尚可，关注持仓板块持续性",
                    confidence="中性",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
        elif today_change < -1.5:
            return AIPrediction(
                trend="明显回调", trend_emoji="📉",
                advice=f"持仓明显回调{market_ref}，短期跌幅较大，市场情绪偏谨慎，关注下方支撑位",
                confidence="谨慎",
                est_nav=est_nav, est_change=est_change, est_time="",
                est_vs_last=0.0, today_verdict=today_verdict,
                valuation_position=valuation_position,
                cost_performance=cost_performance,
                key_levels=key_levels,
                risk_level=risk_level,
                return_7d=return_7d,
                return_1m=return_1m,
                return_6m=return_6m
            )
        elif today_change < -0.5:
            if consecutive_down >= 2:
                return AIPrediction(
                    trend="连续走弱", trend_emoji="🔻",
                    advice=f"已连续{consecutive_down}日下跌{market_ref}，短期弱势，关注基本面是否有变化",
                    confidence="谨慎",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
            else:
                return AIPrediction(
                    trend="小幅调整", trend_emoji="📉",
                    advice=f"基金今日小幅调整{market_ref}，短期波动，关注持仓板块和后市走向",
                    confidence="中性",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
        else:
            if consecutive_up >= 3:
                return AIPrediction(
                    trend="连续上涨", trend_emoji="📈",
                    advice=f"已连续{consecutive_up}日微涨{market_ref}，趋势稳中有升，动能温和释放",
                    confidence="乐观",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )
            else:
                return AIPrediction(
                    trend="震荡整理", trend_emoji="➡️",
                    advice=f"基金今日震荡整理{market_ref}，方向不明，保持耐心，等待趋势信号",
                    confidence="中性",
                    est_nav=est_nav, est_change=est_change, est_time="",
                    est_vs_last=0.0, today_verdict=today_verdict,
                    valuation_position=valuation_position,
                    cost_performance=cost_performance,
                    key_levels=key_levels,
                    risk_level=risk_level,
                    return_7d=return_7d,
                    return_1m=return_1m,
                    return_6m=return_6m
                )


async def fetch_fund_from_eastmoney(fund_code: str, stock_index: Optional[IndexInfo] = None, bond_index: Optional[IndexInfo] = None, hs300_index: Optional[IndexInfo] = None, force: int = 0, lite: int = 0) -> Optional[FundInfo]:
    """
    从天天基金网 (fund.eastmoney.com) 获取基金净值数据
    同时从 fundgz API 获取当日估算净值
    stock_index: 上证指数（用于股票/指数基金参考）
    bond_index: 国债指数（用于债券基金参考）
    force: 1=强制刷新（拉所有）；0=复用缓存
    非盘中 + force=1 时跳过 est_url（拉不到新数据，用 est_cache）
    """
    realtime_url = f"https://fund.eastmoney.com/{fund_code}.html"
    # 估算净值 API（JSONP格式）
    est_url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js?rt=1"
    skip_est = (force == 1) and (not is_trading_time())  # 非盘中强制刷新时跳过 est 拉取

    try:
        # 并行获取：主页 + 估算净值（非盘中可跳过估算）
        if skip_est:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp_page = await client.get(realtime_url, headers=HTTP_HEADERS)
                html = resp_page.text
            est_text = ""  # 非盘中不解析估算
        else:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp_page, resp_est = await asyncio.gather(
                    client.get(realtime_url, headers=HTTP_HEADERS),
                    client.get(est_url, headers={**HTTP_HEADERS, "Referer": "https://fund.eastmoney.com/"}, timeout=8.0)
                )
                html = resp_page.text
                est_text = resp_est.text

        # ---- 主页数据 ----
        name_match = re.search(r"<title>([^(（\s]+)", html)
        fund_name = name_match.group(1).strip() if name_match else fund_code

        date_match = re.search(r'fix_date">\((\d{2}-\d{2})\)：', html)
        if not date_match:
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', html)
            nav_date = date_match.group(1) if date_match else "未知"
        else:
            nav_date = f"2026-{date_match.group(1)}"

        nav_match = re.search(r'fix_dwjz[^>]*>([\d.]+)<', html)
        if not nav_match:
            return None
        current_nav = float(nav_match.group(1))

        zzl_match = re.search(r'fix_zzl[^>]*>([-\d.]+)%<', html)
        if zzl_match:
            daily_change = float(zzl_match.group(1))
        else:
            daily_change = 0.0

        if daily_change != 0:
            previous_nav = round(current_nav / (1 + daily_change / 100), 4)
        else:
            previous_nav = current_nav

        # Recalculate actual day change from the latest NAV history.
        # Some Eastmoney fund pages refresh DWJZ/FSRQ before fix_zzl, leaving
        # daily_change stuck on the previous trading day.
        try:
            latest_history = await fetch_fund_history(fund_code, days=3, force=force)
            if latest_history and len(latest_history) >= 2:
                latest_history = sorted(latest_history, key=lambda x: x.date)
                match_idx = next((i for i, item in enumerate(latest_history) if item.date == nav_date), -1)
                if match_idx > 0:
                    prev_item = latest_history[match_idx - 1]
                    if prev_item.nav > 0:
                        previous_nav = round(prev_item.nav, 4)
                        daily_change = round((current_nav - prev_item.nav) / prev_item.nav * 100, 2)
        except Exception as e:
            print(f"recalculate fund daily_change failed for {fund_code}: {e}")

        type_match = re.search(r'类型：</span><[^>]*>([^<]+)<', html)
        fund_type = type_match.group(1).strip() if type_match else "未知"
        
        # 备用：如果类型解析失败，从基金名称推断类型
        if fund_type == "未知":
            name_lower = fund_name.lower()
            if any(k in name_lower for k in ['债券', '债', '纯债', '信用债', '利率债']):
                fund_type = "债券型"
            elif any(k in name_lower for k in ['指数', 'etf', '联接', '增强', '沪深', '上证', '科创', '中证']):
                fund_type = "指数型"
            elif any(k in name_lower for k in ['股票', '成长', '价值', '灵活配置', '混合']):
                fund_type = "混合型"

        # ---- 估算净值数据 ----
        est_nav = 0.0
        est_change = 0.0
        est_time = ""
        # 修正后的估值（应用残差修正模型）
        corrected_est_change = 0.0
        correction_info = get_effective_correction(fund_code)  # {offset, std, confidence, sample_count, enabled}
        # 基金专用模型输出（先定义，估算分支会覆盖）
        model_type = FUND_SPECIFIC_MODELS.get(fund_code, {}).get("type", "residual_only")
        model_estimated_change = 0.0
        model_benchmark_change = 0.0
        model_benchmark_name = ""
        # 今天日期：用于 est_cache 跨日校验
        today_str = datetime.now().strftime("%Y-%m-%d")

        if should_fetch_estimation():
            cached = get_cached_est(fund_code)
            cached_nav = cached.get("est_nav", 0) if cached else 0

            # 交易时间内每次都刷新；收盘后有缓存就用缓存
            # 特殊：20:00 后且当天还没有残差样本时，强制 fetch 一次以记录残差
            need_force_residual = (
                datetime.now().hour >= 20
                and daily_change != 0
                and not has_today_residual(fund_code, nav_date)
            )
            need_fetch = is_trading_time() or cached_nav <= 0 or need_force_residual

            if need_fetch:
                try:
                    m = re.search(r'jsonpgz\((.*)\)', est_text, re.S)
                    if m:
                        est_data = json.loads(m.group(1))
                        gsz = est_data.get('gsz', '')
                        gszzl = est_data.get('gszzl', '')
                        est_time = est_data.get('gztime', '')
                        if gsz and gsz not in ('null', '-'):
                            est_nav = round(float(gsz), 4)
                        if gszzl and gszzl not in ('null', '-'):
                            est_change = round(float(gszzl), 3)  # 提高精度，避免±0.01%被四舍五入掉
                        # === 残差修正 ===
                        # 原始 gsz 估值先保存，用于 20:00+ 时的残差记录
                        raw_est_change_for_record = est_change
                        if est_change != 0 and correction_info["enabled"]:
                            # 修正公式：corrected = gsz + mean_residual
                            corrected_est_change = round(est_change + correction_info["offset"], 3)
                        else:
                            corrected_est_change = est_change
                        # 20:00 后记录残差用于自学习，但**保留 gsz 估值用于对比**
                        if daily_change != 0 and datetime.now().hour >= 20:
                            record_residual(fund_code, raw_est_change_for_record, daily_change, nav_date)
                            # finalize 当天所有盘中快照（多个时点都会产生残差）
                            finalize_day_snapshots(nav_date, {fund_code: daily_change})
                    # === 基金专用模型（指数跟随 / 多因子） ===
                    # 委托 compute_fund_specific_model 统一处理（指数实时跟盘 + 残差修正）
                    m_est, m_bench, m_name = await compute_fund_specific_model(
                        fund_code, daily_change, nav_date
                    )
                    if m_est is not None:
                        model_estimated_change = m_est
                    if m_bench is not None:
                        model_benchmark_change = m_bench
                    if m_name:
                        model_benchmark_name = m_name
                    if est_nav > 0:
                        set_cached_est(fund_code, est_nav, est_change, est_time)
                except Exception:
                    pass
            else:
                # 盘中 9:30-15:00 内 cached_nav > 0 时复用 cache
                # 必须检查 _est_date：今天盘中拉过的 cache 才是有效的；
                # 跨日（_est_date != 今天）的旧 cache 不能用，否则会显示"昨天 11:30"当"今天"
                cached_date = cached.get("_est_date", "")
                if cached_date == today_str and cached.get("est_nav", 0) > 0:
                    est_nav = cached["est_nav"]
                    est_change = cached["est_change"]
                    est_time = cached["est_time"]
                # 缓存场景：重新计算修正值（offset 可能已更新）
                if est_change != 0 and correction_info["enabled"]:
                    corrected_est_change = round(est_change + correction_info["offset"], 3)
                else:
                    corrected_est_change = est_change
                # 缓存路径也调基金专用模型（指数/多因子实时跟盘）—— 收盘后仍有意义
                m_est, m_bench, m_name = await compute_fund_specific_model(
                    fund_code, daily_change, nav_date
                )
                if m_est is not None:
                    model_estimated_change = m_est
                if m_bench is not None:
                    model_benchmark_change = m_bench
                if m_name:
                    model_benchmark_name = m_name
        else:
            # 非交易时段 / 周末：直接调模型（不依赖 est_cache 是否同日）
            # 即使 est_cache 是昨日的，模型估值用的是实时指数 + 残差，仍有意义
            m_est, m_bench, m_name = await compute_fund_specific_model(
                fund_code, daily_change, nav_date
            )
            if m_est is not None:
                model_estimated_change = m_est
            if m_bench is not None:
                model_benchmark_change = m_bench
            if m_name:
                model_benchmark_name = m_name
            # 盘末 15:00 后：读 est_cache（仅当 _est_date == 今天才用）
            # - 今天盘中拉过（_est_date == today）→ est_change 是 15:00 那一刻的锁定值
            # - 今天盘中没拉过（_est_date != today）→ 不用旧 cache，避免显示"昨天 11:30"当"今天"
            cached = get_cached_est(fund_code)
            if cached:
                cached_date = cached.get("_est_date", "")
                if cached_date == today_str and cached.get("est_nav", 0) > 0:
                    est_nav = cached["est_nav"]
                    est_change = cached["est_change"]
                    est_time = cached["est_time"]
                    if est_change != 0 and correction_info["enabled"]:
                        corrected_est_change = round(est_change + correction_info["offset"], 3)
                    else:
                        corrected_est_change = est_change

        # === 三个独立 API 并行（period_returns / history / holdings 都不依赖彼此） ===
        # lite=1 为首页首包：跳过基金详情页才需要的周期、历史、持仓明细。
        if lite:
            period_returns, history, holdings = {}, latest_history if "latest_history" in locals() else [], []
        else:
            # force 透传：force=1 时 period_returns / history 绕过 60s 子缓存
            period_returns_task = fetch_fund_period_returns(fund_code, force=force)
            # history 拉 30 天（4 页 → 1 页，从 ~2s → ~0.5s/基金；实际只用到 30 天）
            history_task = fetch_fund_history(fund_code, days=30, force=force)
            holdings_task = fetch_fund_holdings(fund_code)
            period_returns, history, holdings = await asyncio.gather(
                period_returns_task, history_task, holdings_task
            )

        # === 成本净值与收益计算 ===
        # 规则1: 用户已通过"确定买入"接口标记持仓 -> 用买入当日净值+日期作为成本
        # 规则2: 用户未手动买入 -> 用HISTORICAL_YIELDS配置反推成本作为"虚拟基准"，展示买点信号
        hist_yield_data = HISTORICAL_YIELDS.get(fund_code)
        hist_yield = hist_yield_data["yield"] if isinstance(hist_yield_data, dict) else (hist_yield_data or 0.0)

        holding_record = COST_NAVS.get(fund_code)
        is_user_holding = bool(holding_record and holding_record.get("is_holding", False) and holding_record.get("buy_nav", 0) > 0)
        holding_shares = float(holding_record.get("shares", 1.0) or 1.0) if holding_record else 0.0
        realized_yield_pct = float(holding_record.get("realized_yield_pct", 0.0) or 0.0) if holding_record else 0.0
        transactions = holding_record.get("transactions", []) if holding_record else []

        if is_user_holding:
            # 已确定买入：以用户买入价为成本基准
            cost_nav = float(holding_record.get("buy_nav", 0.0) or 0.0)
            buy_date = holding_record.get("buy_date", "")
            stored_holding_yield = 0.0
            for _yield_key in ("yield_pct", "total_return", "holding_yield_pct", "current_return"):
                try:
                    _yield_value = float(holding_record.get(_yield_key, 0.0) or 0.0)
                except Exception:
                    _yield_value = 0.0
                if abs(_yield_value) > 0.0001:
                    stored_holding_yield = _yield_value
                    break
            if cost_nav <= 0 and current_nav > 0 and abs(stored_holding_yield) > 0.0001:
                cost_nav = round(current_nav / (1 + stored_holding_yield / 100), 4)
            yield_pct = round((current_nav - cost_nav) / cost_nav * 100, 2) if cost_nav > 0 else stored_holding_yield
            if abs(yield_pct) < 0.0001 and abs(stored_holding_yield) > 0.0001:
                yield_pct = round(stored_holding_yield, 2)
            # 已买入的基金不再判断"买点"，而是展示真实持有收益
            can_buy = False
            drop_pct = 0.0

            # 计算"买入以来的每日收益历史"（给前端画收益曲线）
            yield_history = []
            # 粗略计算持有天数
            hold_days = 0
            try:
                if buy_date:
                    d1 = datetime.strptime(buy_date, "%Y-%m-%d")
                    d2 = datetime.strptime(nav_date, "%Y-%m-%d")
                    hold_days = (d2 - d1).days
            except Exception:
                hold_days = 0

            if history:
                # history 按日期升序：保留 buy_date 当日及之后的记录
                for item in history:
                    item_date = item.date
                    # 只保留买入日或之后的净值
                    if buy_date and item_date < buy_date:
                        continue
                    day_yield = round((item.nav - cost_nav) / cost_nav * 100, 2)
                    yield_history.append({
                        "date": item_date,
                        "nav": item.nav,
                        "yield_pct": day_yield,
                        "daily_change": item.change
                    })
                # yield_history 按日期升序（与 history 一致）
            total_return = yield_pct

        else:
            # 未手动买入：使用 BUY_POINT_CONFIG 按基金独立计算买点
            # 规则：以 BUY_POINT_REFS 中保存的 ref_nav 为固定参考起点，
            #   从起点开始累积计算涨跌，涨了距买点变远，跌了距买点变近
            #   ref_nav 只在买入/卖出时更新，不每日变化
            bp_cfg = BUY_POINT_CONFIG.get(fund_code)
            if bp_cfg is not None:
                # === 新规则：按基金独立的下跌阈值 ===
                threshold = float(bp_cfg.get("drop_threshold", DROP_THRESHOLD))

                # 从 BUY_POINT_REFS 读取固定参考起点
                ref_data = BUY_POINT_REFS.get(fund_code, {})
                ref_nav = ref_data.get("ref_nav")
                ref_date = ref_data.get("ref_date", "")

                # 如果没有参考起点，从历史数据中找2026-06-18作为初始参考点
                # 这样初始状态就考虑了从基准日到现在的历史涨跌
                if ref_nav is None or ref_nav <= 0:
                    target_date = "2026-06-18"
                    found_nav = None
                    found_date = None
                    if history:
                        for item in history:
                            if item.date == target_date:
                                found_nav = item.nav
                                found_date = item.date
                                break
                        # 如果找不到0618，用历史数据中最接近的
                        if found_nav is None and len(history) > 0:
                            # 找0618之后最近的交易日
                            for item in history:
                                if item.date >= target_date:
                                    found_nav = item.nav
                                    found_date = item.date
                                else:
                                    break
                    if found_nav is not None:
                        ref_nav = found_nav
                        ref_date = found_date
                    else:
                        # 历史数据不足，用最新净值
                        ref_nav = current_nav
                        ref_date = nav_date
                    
                    BUY_POINT_REFS[fund_code] = {
                        "ref_nav": round(ref_nav, 4),
                        "ref_date": ref_date
                    }
                    save_buy_point_refs(BUY_POINT_REFS)

                # 成本基准（cost_nav）：使用参考起点
                cost_nav = ref_nav
                buy_point_yield = round(-threshold, 2)   # 买点收益率：-5% 或 -1%
                target_nav = round(ref_nav * (1 - threshold / 100), 4)

                # 从参考起点到现在的累计涨跌
                calc_yield = round((current_nav - ref_nav) / ref_nav * 100, 2)
                yield_pct = 0.0  # 未买入时收益率前端不显示
                # 距买点距离：涨了距买点变远，跌了距买点变近
                drop_pct = round(threshold + calc_yield, 2)
                can_buy = calc_yield <= buy_point_yield

                # 进度计算：进度 = 累计跌幅 / 阈值 × 100%
                # 涨了进度=0（还没开始跌），跌了按比例累加
                if threshold > 0:
                    if calc_yield < 0:
                        # 跌了：按跌幅比例计算进度（可以超过100%）
                        progress_pct = round(abs(calc_yield) / threshold * 100, 0)
                    else:
                        # 涨了：进度=0
                        progress_pct = 0

                # 对前端展示：hist_yield 改为 0（新规则下，基准就是参考起点本身）
                hist_yield_for_bp = 0.0
            else:
                # 基金不在新规则中：回退旧逻辑（HISTORICAL_YIELDS + 固定3%）
                if fund_code in COST_NAVS and COST_NAVS[fund_code].get("is_holding", False):
                    cost_nav = COST_NAVS[fund_code]["buy_nav"]
                    yield_pct = round((current_nav - cost_nav) / cost_nav * 100, 2)
                else:
                    ref_nav = history[-1].nav if history else current_nav
                    cost_nav = round(ref_nav / (1 + hist_yield / 100), 4)
                    yield_pct = round((current_nav - cost_nav) / cost_nav * 100, 2)
                buy_point_yield = round(hist_yield - DROP_THRESHOLD, 2)
                drop_pct = round(max(0, yield_pct - buy_point_yield), 2)
                can_buy = yield_pct <= buy_point_yield
                target_nav = round(cost_nav * (1 + buy_point_yield / 100), 4)
                if hist_yield != buy_point_yield:
                    progress_pct = round(min(100, max(0, (hist_yield - yield_pct) / (hist_yield - buy_point_yield) * 100)), 0)
                else:
                    progress_pct = 100 if can_buy else 0
                hist_yield_for_bp = hist_yield
            buy_date = ""
            hold_days = 0
            total_return = 0.0
            yield_history = []

        buy_point = BuyPointInfo(
            cost_nav=cost_nav,
            current_nav=current_nav,
            yield_pct=yield_pct,
            can_buy=can_buy,
            drop_pct=drop_pct,
            is_holding=is_user_holding,
            buy_date=buy_date,
            buy_price=cost_nav,
            hold_days=hold_days,
            total_return=total_return,
            yield_history=yield_history,
            hist_yield=hist_yield_for_bp if not is_user_holding else hist_yield,
            buy_point_yield=buy_point_yield if not is_user_holding else 0.0,
            target_nav=target_nav if not is_user_holding else 0.0,
            progress_pct=progress_pct if not is_user_holding else 0,
            drop_threshold=float(BUY_POINT_CONFIG.get(fund_code, {}).get("drop_threshold", 0.0) or 0.0),
            ref_date=str(BUY_POINT_REFS.get(fund_code, {}).get("ref_date", "")),
            ref_nav=float(BUY_POINT_REFS.get(fund_code, {}).get("ref_nav", 0.0) or 0.0),
            shares=holding_shares if is_user_holding else 0.0,
            realized_yield_pct=realized_yield_pct,
            transactions=transactions
        )

        # 生成AI预判（含估算净值和当日评估 + 市场指数参考）
        fund_type_lower = fund_type.lower()
        is_bond_fund = any(k in fund_type_lower for k in ['债券', '债', '稳健', '纯债', '二级债'])
        ref_index = bond_index if is_bond_fund else stock_index

        # 债券基金估算优化：基于历史相关性动态计算推算系数
        bond_analysis_extra = None
        bond_display_estimate = {}
        if is_bond_fund:
            bond_beta = 0.5  # 默认系数
            bond_r_squared = 0.0  # 相关性强度
            curve_proxy = None

            # 从缓存获取已计算的Beta系数
            beta_cache_key = f"bond_beta_{fund_code}"
            cached_beta = EST_CACHE.get(beta_cache_key, {})
            cached_date = cached_beta.get("_calc_date", "")

            # 每日重新计算一次（或缓存过期时）
            today_str = datetime.now().strftime("%Y-%m-%d")
            if cached_date != today_str or not cached_beta.get("beta"):
                # 动态计算：基于历史净值与国债指数的相关性
                if bond_index and bond_index.history and len(bond_index.history) >= 5 and history and len(history) >= 5:
                    try:
                        # 获取国债指数历史涨跌幅（近7-14天）
                        bond_changes = [h.change for h in bond_index.history[:14] if h.change != 0]
                        # 获取基金历史涨跌幅（同周期）
                        fund_changes = [h.change for h in history[:14] if h.change != 0]

                        # 对齐数据长度
                        min_len = min(len(bond_changes), len(fund_changes))
                        if min_len >= 5:
                            bond_arr = bond_changes[:min_len]
                            fund_arr = fund_changes[:min_len]

                            # 计算相关性系数（简单线性回归：fund_change ≈ beta × bond_change）
                            # Beta = Σ(fund_i × bond_i) / Σ(bond_i × bond_i)
                            sum_fb = sum(f * b for f, b in zip(fund_arr, bond_arr))
                            sum_bb = sum(b * b for b in bond_arr)

                            if sum_bb > 0:
                                calc_beta = sum_fb / sum_bb
                                # 计算R²（相关性强度）
                                fund_mean = sum(fund_arr) / len(fund_arr)
                                ss_tot = sum((f - fund_mean) ** 2 for f in fund_arr)
                                ss_res = sum((f - calc_beta * b) ** 2 for f, b in zip(fund_arr, bond_arr))
                                r_sq = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

                                # 限制Beta范围（债券基金通常在0.1-1.5之间）
                                bond_beta = max(0.1, min(1.5, abs(calc_beta)))
                                bond_r_squared = max(0, min(1, r_sq))

                                # 缓存计算结果
                                EST_CACHE[beta_cache_key] = {
                                    "beta": round(bond_beta, 3),
                                    "r_squared": round(bond_r_squared, 3),
                                    "_calc_date": today_str,
                                    "sample_days": min_len
                                }
                                save_est_cache(EST_CACHE)
                                print(f"[债券Beta计算] {fund_code}: beta={bond_beta:.3f}, R²={bond_r_squared:.3f}, 样本={min_len}天")
                    except Exception as e:
                        print(f"[债券Beta计算失败] {fund_code}: {e}")
            else:
                bond_beta = cached_beta.get("beta", 0.5)
                bond_r_squared = cached_beta.get("r_squared", 0.0)

            # 使用计算出的 Beta 系数推算估算净值：
            # - 债基展示优先用自身趋势模型；
            # - 天天估值只在有效时保留作参考，国债指数按相关性做小权重方向修正。
            if bond_index and bond_index.current > 0:
                curve_proxy = bond_curve_proxy_signal(bond_index)
                direct_signal = round(bond_index.daily_change * bond_beta, 3)
                curve_signal = round(curve_proxy.get("signal", 0.0) * bond_beta, 3)
                bond_signal = round(curve_signal * 0.70 + direct_signal * 0.30, 3)
                baseline_signal = model_estimated_change if abs(float(model_estimated_change or 0.0)) >= 0.001 else 0.0
                bond_weight = 0.26 if bond_r_squared > 0.6 else (0.18 if bond_r_squared > 0.3 else 0.10)
                if curve_proxy.get("quality") == "弱":
                    bond_weight = min(bond_weight, 0.08)
                hybrid_change = round(baseline_signal * (1 - bond_weight) + bond_signal * bond_weight, 3)
                if abs(hybrid_change) < 0.001 and abs(bond_signal) >= 0.001:
                    hybrid_change = round(bond_signal * bond_weight, 3)
                hybrid_change = max(-0.18, min(0.18, hybrid_change))
                # 如果估算净值有数据但涨跌为0，用混合估算补齐
                if est_nav > 0 and est_change == 0:
                    print(f"[债券推算] {fund_code}: baseline={baseline_signal}, 曲线代理={curve_proxy}, 国债信号={bond_signal}, beta={bond_beta:.3f}, weight={bond_weight:.2f}")
                    est_change = hybrid_change
                    if previous_nav > 0:
                        est_nav = round(previous_nav * (1 + est_change / 100), 4)
                    model_estimated_change = est_change
                    model_benchmark_change = bond_index.daily_change
                    model_benchmark_name = f"债基趋势+曲线代理 {bond_weight:.0%}"
                    print(f"[债券推算] {fund_code}: 推算后 est_nav={est_nav}, est_change={est_change}")
                    set_cached_est(fund_code, est_nav, est_change, est_time or datetime.now().strftime("%Y-%m-%d %H:%M"))
                # 如果完全没有估算数据（非交易时间），用昨日净值+国债指数推算
                elif est_nav == 0 and previous_nav > 0:
                    print(f"[债券非交易推算] {fund_code}: baseline={baseline_signal}, 曲线代理={curve_proxy}, 国债信号={bond_signal}, beta={bond_beta:.3f}, weight={bond_weight:.2f}")
                    est_change = hybrid_change
                    est_nav = round(previous_nav * (1 + est_change / 100), 4)
                    model_estimated_change = est_change
                    model_benchmark_change = bond_index.daily_change
                    model_benchmark_name = f"债基趋势+曲线代理 {bond_weight:.0%}"
                    print(f"[债券非交易推算] {fund_code}: 推算后 est_nav={est_nav}, est_change={est_change}")
                    set_cached_est(fund_code, est_nav, est_change, est_time or datetime.now().strftime("%Y-%m-%d %H:%M"))
                elif abs(float(est_change or 0.0)) >= 0.005:
                    # 天天基金给出有效非零估值时，展示仍优先用它；模型字段保留混合估算作对照。
                    model_estimated_change = hybrid_change
                    model_benchmark_change = bond_index.daily_change
                    model_benchmark_name = f"债基趋势+曲线代理 {bond_weight:.0%}"

            # 将Beta系数传递给AI预判（用于置信度判断）
            bond_analysis_extra = {
                "beta": bond_beta,
                "r_squared": bond_r_squared,
                "correlation_quality": "强相关" if bond_r_squared > 0.6 else ("中等相关" if bond_r_squared > 0.3 else "弱相关")
            }
            bond_display_estimate = build_bond_range_estimate(
                fund_code=fund_code,
                model_estimated_change=model_estimated_change,
                bond_index=bond_index,
                curve_proxy=curve_proxy,
                beta=bond_beta,
                r_squared=bond_r_squared,
            )

        # period_returns / history / holdings 已在上面一次性并行拉取（line ~2234）

        # === 盘末行为：est_cache 锁定 15:00 那一刻的估算值 ===
        # 业务语义：
        # - 盘中 9:30-15:00：fundgz 实时拉 → est_change 随盘中估值变化（前端 AI 卡片实时更新）
        # - 15:00 那一刻：最后一次 fetch 把 est_change 写入 est_cache（持久化）
        # - 15:00 后 should_fetch_estimation=False：不 fetch，但 fallback 分支读 est_cache 拿到 15:00 锁定值
        # - 下一交易日 9:30 后：should_fetch_estimation=True → fetch 拉新值覆盖 est_cache
        # 因此盘中 fetch 写 est_cache、盘末 fallback 读 est_cache = 自动"freeze 在 15:00 那一刻"
        # 不需要在这里手动置 0 或覆盖

        ai_pred = generate_ai_prediction_simple(
            fund_type=fund_type,
            fund_name=fund_name,
            daily_change=daily_change,
            history=history,
            cost_nav=cost_nav,
            current_nav=current_nav,
            est_nav=est_nav,
            est_change=est_change,
            market_index=ref_index,
            bond_extra=bond_analysis_extra if is_bond_fund else None,
            period_returns=period_returns
        )

        # 获取买点起始日期
        ref_date = BUY_POINT_REFS.get(fund_code, {}).get("ref_date", "")

        # 基金持仓（已在上面并行拉取）

        # 计算区间累计收益率（基于近 130 个交易日 history）作为兜底
        def _period_return(offset_days: int) -> float:
            if not history or current_nav <= 0:
                return 0.0
            try:
                latest_date = datetime.strptime(history[0].date[:10], "%Y-%m-%d")
                target_date = latest_date - timedelta(days=offset_days)
                base_item = next(
                    (item for item in history if datetime.strptime(item.date[:10], "%Y-%m-%d") <= target_date),
                    history[-1]
                )
            except Exception:
                base_item = history[min(offset_days, len(history) - 1)]
            old_nav = base_item.nav
            if old_nav > 0:
                return round((current_nav - old_nav) / old_nav * 100, 2)
            return 0.0

        def _r(key: str) -> float:
            v = period_returns.get(key)
            if v is None:
                return _period_return({"Z": 7, "Y": 30, "3Y": 90, "6Y": 180, "1N": 365}.get(key, 7))
            return round(v, 2)

        # ===== 收盘后锁定当日快照（gsz/model）=====
        # 目的：15:00 收盘后反复刷新页面，AI 卡片的估算结果保持一致；
        #      盘中 9:30-15:00 始终实时计算，避免 14:30 后估算不再更新。
        # 关键：快照只锁定估算值，实际收益永远用最新净值和上一交易日净值重新计算。
        now = datetime.now()
        # snap_key 选择规则：
        # - 交易日 9:30 之后 → 用今天系统日期（避免 nav_date 停留在上周五导致周一开盘后 snapshot 跨日不复位）
        # - 其他时段（周末 / 工作日 0:00-9:30）→ 用 nav_date（与原 hard constraint 行为一致）
        is_trading_now = now.weekday() < 5 and (now.hour, now.minute) >= (9, 30)
        snap_key = now.strftime("%Y-%m-%d") if is_trading_now else (nav_date or now.strftime("%Y-%m-%d"))
        fund_entry_lock = CORRECTION_CACHE.setdefault(fund_code, {"samples": []})
        snap = fund_entry_lock.get("daily_snapshot")
        if (not is_trading_time()) and snap and snap.get("date") == snap_key:
            # 复用当日估算快照（gsz/model），不覆盖 actual/daily_change。
            snap_gsz = snap.get("gsz")
            snap_model = snap.get("model")
            # 旧快照里经常会出现 gsz/model=0.0。0 只能表示“当时没取到有效估值”，
            # 不能覆盖刚刚按指数模型重新算出的盘中预估，否则前端会全部显示 +0.00%。
            if snap_gsz is not None and abs(float(snap_gsz or 0)) >= 0.005:
                est_change = snap_gsz
            if snap_model is not None and abs(float(snap_model or 0)) >= 0.005:
                model_estimated_change = snap_model
        elif (not is_trading_time()) and (now.hour >= 15):
            # 收盘后第一次：写入估算快照。actual 不入快照，避免净值披露前把昨日实际锁成今日实际。
            fund_entry_lock["daily_snapshot"] = {
                "date": snap_key,
                "gsz": round(est_change, 3),
                "model": round(model_estimated_change, 3),
                "nav_date": nav_date,
                "save_time": now.strftime("%H:%M")
            }

        display_estimated_change = 0.0
        if model_type == "bond_baseline":
            # 债基 gsz 盘中经常为 0 或不稳定，展示优先使用自身趋势模型。
            _display_candidates = (model_estimated_change, corrected_est_change, est_change, get_last_est_change(fund_code))
        elif model_type in ("index_following", "multi_factor"):
            _display_candidates = (model_estimated_change, corrected_est_change, est_change, get_last_est_change(fund_code))
        else:
            _display_candidates = (corrected_est_change, model_estimated_change, est_change, get_last_est_change(fund_code))
        for _est_value in _display_candidates:
            try:
                _est_num = float(_est_value or 0.0)
            except Exception:
                _est_num = 0.0
            if abs(_est_num) >= 0.005:
                display_estimated_change = round(_est_num, 3)
                break
            if display_estimated_change == 0.0 and _est_value is not None:
                display_estimated_change = round(_est_num, 3)

        ensure_added_fund_tracking_baseline(fund_code, current_nav, nav_date)

        return FundInfo(
            code=fund_code,
            name=fund_name,
            type=fund_type,
            current_nav=current_nav,
            previous_nav=previous_nav,
            nav_date=nav_date,
            daily_change=round(daily_change, 2),
            estimated=est_nav > 0,
            estimated_nav=est_nav,
            estimated_change=display_estimated_change,
            estimated_time=est_time,
            history=history,
            buy_point=buy_point,
            ai_prediction=ai_pred,
            buy_point_ref_date=ref_date,
            holdings=holdings,
            # 上一交易日预估（未开盘时显示用，从 correction_cache 取最近一次 est_change）
            prev_est_change=get_last_est_change(fund_code),
            # 残差修正模型输出
            corrected_estimated_change=corrected_est_change,
            correction_offset=correction_info["offset"],
            correction_std=correction_info["std"],
            correction_confidence=correction_info["confidence"],
            correction_sample_count=correction_info["sample_count"],
            # 基金专用模型输出
            model_type=model_type,
            model_estimated_change=model_estimated_change,
            model_benchmark_change=model_benchmark_change,
            model_benchmark_name=model_benchmark_name,
            model_offset=get_index_model_estimate(fund_code)["offset"],
            model_std=get_index_model_estimate(fund_code)["std"],
            model_confidence=get_index_model_estimate(fund_code)["confidence"],
            model_sample_count=get_index_model_estimate(fund_code)["sample_count"],
            model_enabled=get_index_model_estimate(fund_code)["enabled"],
            bond_estimate=bond_display_estimate,
            # 区间收益（从天天基金 API 抓取，无数据时兜底用 history 计算）
            return_7d=_r("Z"),
            return_1m=_r("Y"),
            return_3m=_r("3Y"),
            return_6m=_r("6Y")
        )

    except Exception as e:
        print(f"获取基金 {fund_code} 数据失败: {e}")
        return None


async def fetch_market_news(force: bool = False) -> List[NewsItem]:
    """
    获取重点股市资讯（新浪财经滚动新闻），列表每 10 分钟重新抓取一次。
    筛选 A股/指数/科创/债券/政策/基金相关内容，降低泛财经噪音。
    """
    # === 10 分钟缓存 ===
    now_ts = time.time()
    fetched_at = datetime.now().strftime("%m-%d %H:%M")
    if (not force) and NEWS_CACHE["data"] is not None and (now_ts - NEWS_CACHE.get("saved_at", 0.0)) < NEWS_LIST_CACHE_TTL:
        return NEWS_CACHE["data"]

    # 新浪 7x24 快讯比滚动新闻更新更及时，先抓它；滚动新闻作为兜底。
    live_news_sources = [
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0",
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=2&page_size=50&zhibo_id=152&tag_id=0",
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=3&page_size=50&zhibo_id=152&tag_id=0",
    ]

    def _json_from_text(text: str) -> dict:
        """兼容 JSON / JSONP 返回。"""
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        return {}

    def _strip_html(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text or "")
        return re.sub(r"\s+", " ", text).strip()

    def _walk_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from _walk_dicts(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from _walk_dicts(value)

    def _parse_time(raw) -> tuple:
        if raw is None:
            return "", 0
        try:
            ts = int(float(raw))
            if ts > 10_000_000_000:
                ts = ts // 1000
            return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), ts
        except Exception:
            pass
        text = str(raw).strip()
        candidates = []
        m = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", text)
        if m:
            candidates.append((m.group(0), "%Y-%m-%d %H:%M:%S"))
        m = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text)
        if m:
            candidates.append((m.group(0), "%Y-%m-%d %H:%M"))
        m = re.search(r"\d{2}-\d{2}\s+\d{2}:\d{2}", text)
        if m:
            candidates.append((m.group(0), "%m-%d %H:%M"))
        for value, fmt in candidates:
            try:
                dt = datetime.strptime(value, fmt)
                if fmt.startswith("%m"):
                    dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%m-%d %H:%M"), int(dt.timestamp())
            except Exception:
                continue
        return text[:16], 0

    def _event_date_from_title(title: str) -> str:
        """提取未来一周明确事件日期，支持 7月29日 / 7/31 / 下周一。"""
        base = datetime.now()
        text = title or ""
        m = re.search(r"(\d{1,2})[月/\.-](\d{1,2})[日号]?", text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = base.year + (1 if month < base.month - 1 else 0)
            try:
                dt = datetime(year, month, day)
                if 0 <= (dt.date() - base.date()).days <= 7:
                    return dt.strftime("%Y-%m-%d")
            except Exception:
                return ""
        week_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        m = re.search(r"下周([一二三四五六日天])", text)
        if m:
            days = 7 - base.weekday() + week_map[m.group(1)]
            if 0 <= days <= 7:
                return (base + timedelta(days=days)).strftime("%Y-%m-%d")
        return ""

    def _enrich_item(item: NewsItem, priority: int) -> NewsItem:
        item.importance = int(priority)
        event_date = _event_date_from_title(item.title)
        if event_date:
            item.event_date = event_date
            if "重点日" not in item.tags:
                item.tags = ["重点日"] + (item.tags or [])
        return item

    async def _collect_live_news():
        items = []
        for url in live_news_sources:
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    r = await client.get(url, headers=HTTP_HEADERS)
                    r.raise_for_status()
                    data = _json_from_text(r.text)
                for item in _walk_dicts(data):
                    title = _strip_html(item.get("rich_text") or item.get("content") or item.get("title") or item.get("summary") or "")
                    if not title or len(title) < 8:
                        continue
                    link = item.get("url") or item.get("link") or item.get("docurl") or "https://finance.sina.com.cn/7x24/"
                    raw_time = item.get("create_time") or item.get("created_at") or item.get("ctime") or item.get("time") or item.get("timestamp")
                    time_str, ctime_ts = _parse_time(raw_time)
                    if not time_str:
                        time_str = fetched_at
                    items.append((title, link, time_str, ctime_ts))
            except Exception as e:
                print(f"从新浪7x24获取资讯失败: {e}")
        return items

    async def _collect_eastmoney_news():
        """东方财富快讯：103/104 偏股市证券，109 偏基金。"""
        items = []
        columns = ["103", "104", "109"]
        base_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        for column in columns:
            try:
                params = {
                    "client": "web",
                    "biz": "web_724",
                    "fastColumn": column,
                    "sortEnd": "",
                    "pageSize": "50",
                    "req_trace": str(int(time.time() * 1000)),
                }
                headers = {**HTTP_HEADERS, "Referer": "https://kuaixun.eastmoney.com/"}
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    r = await client.get(base_url, params=params, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                for item in data.get("data", {}).get("fastNewsList", []):
                    title = _strip_html(item.get("title") or item.get("summary") or "")
                    if not title or len(title) < 8:
                        continue
                    time_str, ctime_ts = _parse_time(item.get("showTime") or item.get("showTimeStr") or item.get("ctime"))
                    link = item.get("url") or item.get("shareUrl") or "https://kuaixun.eastmoney.com/"
                    items.append((title, link, time_str or fetched_at, ctime_ts, column))
            except Exception as e:
                print(f"从东方财富快讯栏目{column}获取资讯失败: {e}")
        return items

    # 新浪财经滚动新闻接口lid=2516(财经)/1686(股票)/135(首页)
    news_sources = [
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=50&page=1",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=50&page=2",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=50&page=3",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=50&page=1",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=50&page=2",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=135&num=40&page=1",
    ]

    all_news = []
    fallback_news = []
    opinion_news = []
    seen_titles = set()

    def _push_news(pool, priority: int, ctime_ts: int, item: NewsItem, strict_event: bool = False):
        if is_opinion_news(item.title) or (strict_event and not is_hard_event_news(item.title)):
            item.tags = ["观点汇总", "7x24观点"]
            opinion_news.append((ctime_ts, item))
            return
        pool.append((priority, ctime_ts, item))

    for title, link, time_str, ctime_ts, column in await _collect_eastmoney_news():
        if title in seen_titles:
            continue
        if any(x in title for x in ['下载', '手机', 'APP', '开户', '登录', '广告']):
            continue
        sentiment, tags = classify_news(title)
        importance = news_importance_score(title)
        source_tag = "基金" if column == "109" else "股市"
        news_item = NewsItem(
            title=title[:180],
            url=link,
            time=time_str,
            fetched_at=fetched_at,
            sentiment=sentiment,
            tags=tags or [source_tag]
        )
        seen_titles.add(title)
        # 东财股市/基金栏目是目标资讯源，即使关键词少也优先进入候选池。
        _push_news(all_news, importance + 4, ctime_ts, _enrich_item(news_item, importance + 4), strict_event=True)

    for title, link, time_str, ctime_ts in await _collect_live_news():
        if title in seen_titles:
            continue
        if any(x in title for x in ['下载', '手机', 'APP', '开户', '登录', '广告']):
            continue
        sentiment, tags = classify_news(title)
        importance = news_importance_score(title)
        news_item = NewsItem(
            title=title[:180],
            url=link,
            time=time_str,
            fetched_at=fetched_at,
            sentiment=sentiment,
            tags=tags or ["7x24"]
        )
        seen_titles.add(title)
        # 7x24 快讯本身就是“新鲜度”源，弱相关也作为候选兜底，避免晚间无新闻。
        target = all_news if importance > 0 else fallback_news
        _push_news(target, importance + 2, ctime_ts, _enrich_item(news_item, importance + 2), strict_event=True)

    for url in news_sources:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                r = await client.get(url, headers=HTTP_HEADERS)
                r.raise_for_status()
                data = r.json()

            items = data.get('result', {}).get('data', [])
            for item in items:
                title = item.get('title', '').strip()
                link = item.get('url', '')
                ctime = item.get('ctime', '')

                if not title or not link:
                    continue
                if len(title) < 8 or len(title) > 140:
                    continue
                if title in seen_titles:
                    continue
                # 过滤广告/无关内容
                if any(x in title for x in ['下载', '手机', 'APP', '开户', '登录', '广告']):
                    continue
                # 转换时间戳为日期
                time_str = ""
                ctime_ts = 0
                if ctime:
                    try:
                        ctime_ts = int(ctime)
                        dt_obj = datetime.fromtimestamp(ctime_ts)
                        time_str = dt_obj.strftime("%m-%d %H:%M")
                    except:
                        pass

                # 情绪 + 事件标签判别
                sentiment, tags = classify_news(title)
                importance = news_importance_score(title)

                news_item = NewsItem(
                    title=title,
                    url=link,
                    time=time_str,
                    fetched_at=fetched_at,
                    sentiment=sentiment,
                    tags=tags or ["市场"]
                )

                if importance <= 0:
                    # 如果重点关键词不足 10 条，用股票/财经源中的市场新闻补足，避免前端长期只有几条。
                    if len(fallback_news) < 40:
                        _push_news(fallback_news, importance, ctime_ts, _enrich_item(news_item, importance))
                        seen_titles.add(title)
                    continue
                seen_titles.add(title)

                _push_news(all_news, importance, ctime_ts, _enrich_item(news_item, importance))

        except Exception as e:
            print(f"从 {url[:60]} 获取资讯失败: {e}")
            continue

    # === 写入 10 分钟缓存 ===
    # 最后一层再过滤一次，避免“称/表示/预计/研报/点评”等评论类内容漏进主列表。
    merged_news = sorted(all_news + fallback_news, key=lambda x: (x[1], x[0]), reverse=True)
    event_news = []
    for priority, ctime_ts, item in merged_news:
        if is_opinion_news(item.title) or any("观点" in str(tag) for tag in (item.tags or [])):
            item.tags = ["观点汇总", "7x24观点"]
            opinion_news.append((ctime_ts, item))
            continue
        if (not item.event_date) and (not is_hard_event_news(item.title)) and priority < 5:
            item.tags = ["观点汇总", "7x24观点"]
            opinion_news.append((ctime_ts, item))
            continue
        event_news.append((priority, ctime_ts, item))

    event_candidates = []
    used_titles = set()
    for priority, ctime_ts, item in event_news:
        if item.event_date or any(kw in item.title for kw in ["长鑫", "美联储", "议息", "A50", "交割", "上市", "财报", "业绩"]):
            event_candidates.append((priority + 6, ctime_ts, item))

    result = []
    for _, _, item in sorted(event_candidates, key=lambda x: (x[0], x[1]), reverse=True)[:3]:
        if item.title not in used_titles:
            result.append(item)
            used_titles.add(item.title)
    for _, _, item in event_news:
        if len(result) >= 80:
            break
        if item.title not in used_titles:
            result.append(item)
            used_titles.add(item.title)

    if not result:
        # 如果严格事件筛选为空，退回到最新的非广告市场资讯，避免前端长期只有“后台更新中”。
        relaxed_news = []
        for priority, ctime_ts, item in merged_news:
            if item.title in used_titles:
                continue
            if is_opinion_news(item.title) and len(relaxed_news) >= 6:
                continue
            relaxed_news.append((priority, ctime_ts, item))
            used_titles.add(item.title)
            if len(relaxed_news) >= 80:
                break
        result = [item for _, _, item in relaxed_news]

    result = result if result else [
        NewsItem(title="暂无重点股市资讯，稍后自动刷新", url="https://finance.sina.com.cn/", time=datetime.now().strftime("%m-%d %H:%M"), fetched_at=fetched_at, tags=["股市"]),
    ]
    if opinion_news:
        latest_opinions = sorted(opinion_news, key=lambda x: x[0], reverse=True)[:12]
        opinion_titles = []
        seen_opinion_titles = set()
        for _, item in latest_opinions:
            title = item.title.strip()
            if not title or title in seen_opinion_titles:
                continue
            seen_opinion_titles.add(title)
            opinion_titles.append(title)
            if len(opinion_titles) >= 5:
                break
        opinion_item = NewsItem(
            title=("7x24观点汇总：" + "；".join(opinion_titles))[:220],
            url="https://finance.sina.com.cn/7x24/",
            time=latest_opinions[0][1].time or fetched_at,
            fetched_at=fetched_at,
            sentiment="neutral",
            tags=["观点汇总", "7x24观点"]
        )
        result.append(opinion_item)
    result = sorted(result, key=lambda item: _parse_time(item.time)[1], reverse=True)[:80]
    NEWS_CACHE["data"] = result
    NEWS_CACHE["saved_at"] = now_ts
    return result


def _avg_numbers(values) -> Optional[float]:
    nums = []
    for value in values:
        try:
            if value is not None:
                nums.append(float(value))
        except Exception:
            continue
    if not nums:
        return None
    return round(sum(nums) / len(nums), 3)


def _theme_tone(value: Optional[float] = None, label: str = "") -> str:
    if value is not None:
        if value > 0:
            return "good"
        if value < 0:
            return "bad"
    if label in ("偏暖", "偏强"):
        return "good"
    if label in ("偏紧", "偏弱"):
        return "bad"
    return "neutral"


def _find_theme_fund(funds: List[FundInfo], *patterns: str) -> Optional[FundInfo]:
    for fund in funds or []:
        text = f"{fund.code} {fund.name} {fund.model_benchmark_name}"
        if any(re.search(pattern, text) for pattern in patterns):
            return fund
    return None


def _history_rows(history: Optional[List[IndexHistoryItem]]) -> list[dict]:
    rows = []
    for item in history or []:
        try:
            rows.append({
                "date": item.date,
                "close": float(item.close),
                "change": float(item.change),
            })
        except Exception:
            continue
    return sorted(rows, key=lambda x: x["date"])


def _history_return(history: Optional[List[IndexHistoryItem]], days: int) -> Optional[float]:
    rows = _history_rows(history)
    if len(rows) < 2:
        return None
    latest = rows[-1]
    try:
        cutoff = datetime.strptime(latest["date"], "%Y-%m-%d") - timedelta(days=days)
    except Exception:
        return None
    base = None
    for row in rows:
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d")
        except Exception:
            continue
        if d <= cutoff:
            base = row
    if base is None and days <= 7 and len(rows) >= 2:
        base = rows[0]
    if not base or base["close"] <= 0 or base["date"] == latest["date"]:
        return None
    return round((latest["close"] / base["close"] - 1) * 100, 2)


def _theme_detail_text(name: str, value: Optional[float], note: str, source: str) -> str:
    if value is None:
        return f"{name}暂无完整行情，等待后台下一轮更新。"
    direction = "偏强" if value > 0 else "偏弱" if value < 0 else "震荡"
    if "半导体" in name or "科创" in name:
        subject = "科创50和芯片主题"
    elif "科技" in name:
        subject = "深证与科创成长风格"
    elif "宽基" in name:
        subject = "A股主要宽基指数"
    elif "蓝筹" in name or "上证50" in name:
        subject = "上证50权重板块"
    elif "债" in name or "利率" in name:
        subject = "利率与债券资产"
    elif name in ("医疗", "白酒", "新能源", "消费", "券商", "银行"):
        subject = f"{name}行业指数"
    else:
        subject = note or source or name
    return f"{subject}当前{direction}，用于观察主题热度和相关基金短期压力。"


def _theme_sector_from_history(
    name: str,
    value: Optional[float],
    label: str,
    note: str,
    source: str,
    history: Optional[List[IndexHistoryItem]] = None,
    detail_type: str = "index",
) -> ThemeSectorInfo:
    return ThemeSectorInfo(
        name=name,
        value=value,
        label=label,
        note=note,
        tone=_theme_tone(value, label),
        source=source,
        updated_at=datetime.now().strftime("%H:%M"),
        history=history or [],
        return_7d=_history_return(history, 7),
        return_1m=_history_return(history, 30),
        return_6m=_history_return(history, 180),
        detail=_theme_detail_text(name, value, note, source),
        detail_type=detail_type,
    )


def _composite_history(histories: list[Optional[List[IndexHistoryItem]]]) -> List[IndexHistoryItem]:
    by_date: dict[str, list[float]] = {}
    for history in histories:
        for row in _history_rows(history):
            if row.get("change") is None:
                continue
            by_date.setdefault(row["date"], []).append(float(row["change"]))
    if not by_date:
        return []
    value = 100.0
    result: List[IndexHistoryItem] = []
    for date in sorted(by_date):
        change = round(sum(by_date[date]) / len(by_date[date]), 2)
        value = value * (1 + change / 100)
        result.append(IndexHistoryItem(date=date, close=round(value, 2), change=change))
    return list(reversed(result))


def _theme_fund_change(fund: Optional[FundInfo]) -> Optional[float]:
    if not fund:
        return None
    try:
        if market_status() == "trading":
            for value in (
                fund.model_estimated_change,
                fund.corrected_estimated_change,
                fund.estimated_change,
            ):
                if value is not None and abs(float(value)) > 0.0001:
                    return round(float(value), 3)
        return round(float(fund.daily_change), 3)
    except Exception:
        return None


def _policy_theme_label(news: List[NewsItem]) -> tuple[str, str]:
    good = bad = total = 0
    keywords = re.compile(r"央行|逆回购|流动性|资金面|LPR|MLF|降准|降息|加息|美联储|利率")
    for item in news or []:
        text = f"{item.title} {' '.join(item.tags or [])} {item.sentiment}"
        if not keywords.search(text):
            continue
        total += 1
        if item.sentiment == "bullish" or re.search(r"降准|降息|流动性.*宽松|资金面.*宽", text):
            good += 1
        elif item.sentiment == "bearish" or re.search(r"加息|收紧|资金面.*紧|利率.*上行", text):
            bad += 1
    if total == 0:
        return "观察", "等待政策/资金面资讯"
    if good > bad:
        return "偏暖", f"政策资金面资讯 {total} 条"
    if bad > good:
        return "偏紧", f"政策资金面资讯 {total} 条"
    return "中性", f"政策资金面资讯 {total} 条"


async def fetch_theme_market_sectors() -> List[ThemeSectorInfo]:
    """拉取独立市场主题指数，用于我的页市场温度，不依赖当前持仓。"""
    quote_tasks = [fetch_generic_index(code, display_name) for _, code, display_name in THEME_MARKET_INDEXES]
    history_tasks = [fetch_index_history_for_code(code, 190) for _, code, _display_name in THEME_MARKET_INDEXES]
    quote_results = await asyncio.gather(*quote_tasks, return_exceptions=True)
    history_results = await asyncio.gather(*history_tasks, return_exceptions=True)
    sectors: List[ThemeSectorInfo] = []
    for (name, code, display_name), result, history_result in zip(THEME_MARKET_INDEXES, quote_results, history_results):
        value: Optional[float] = None
        note = display_name
        history: List[IndexHistoryItem] = []
        if isinstance(result, IndexInfo) and result.current > 0:
            value = round(float(result.daily_change), 2)
            note = f"{display_name} {result.code}"
        if isinstance(history_result, list):
            history = history_result
        sectors.append(_theme_sector_from_history(
            name=name,
            value=value,
            label="观察" if value is None else "",
            note=note,
            source="主题指数",
            history=history,
        ))
    return sectors


def build_theme_sectors(
    funds: List[FundInfo],
    index_info: IndexInfo,
    bond_index: Optional[IndexInfo],
    hs300_index: Optional[IndexInfo],
    sz_index: Optional[IndexInfo],
    news: List[NewsItem],
    market_theme_sectors: Optional[List[ThemeSectorInfo]] = None,
    k50_index: Optional[IndexInfo] = None,
    sh50_index: Optional[IndexInfo] = None,
) -> List[ThemeSectorInfo]:
    """后台生成主题板块温度，随组合数据定时刷新。"""
    now_label = datetime.now().strftime("%H:%M")
    bond_fund = _find_theme_fund(funds, r"020741", r"债", r"国债")
    sh_change = index_info.daily_change if index_info and index_info.current > 0 else None
    sz_change = sz_index.daily_change if sz_index and sz_index.current > 0 else None
    hs300_change = hs300_index.daily_change if hs300_index and hs300_index.current > 0 else None
    k50_change = k50_index.daily_change if k50_index and k50_index.current > 0 else None
    bond_change = bond_index.daily_change if bond_index and bond_index.current > 0 else None
    sh50_change = sh50_index.daily_change if sh50_index and sh50_index.current > 0 else None
    policy_label, policy_note = _policy_theme_label(news)

    raw = [
        ("宽基指数", _avg_numbers([sh_change, sz_change, hs300_change]), "", "上证 / 深证 / 沪深300", "指数行情", _composite_history([index_info.history, sz_index.history if sz_index else [], hs300_index.history if hs300_index else []]), "index"),
        ("科技成长", _avg_numbers([sz_change, k50_change]), "", "深证 + 科创成长情绪", "指数行情", _composite_history([sz_index.history if sz_index else [], k50_index.history if k50_index else []]), "index"),
        ("半导体科创", k50_change, "", "科创50 / 芯片主题", "指数行情", k50_index.history if k50_index else [], "index"),
        ("债券利率", _avg_numbers([bond_change, _theme_fund_change(bond_fund)]), "", "国债指数 + 债基净值", "指数+持仓", bond_index.history if bond_index else [], "index"),
        ("上证50蓝筹", sh50_change, "", "上证50 / 大盘蓝筹", "指数行情", sh50_index.history if sh50_index else [], "index"),
        ("资金政策", None, policy_label, policy_note, "资讯聚合", [], "policy"),
    ]

    sectors: List[ThemeSectorInfo] = []
    for name, value, label, note, source, history, detail_type in raw:
        item = _theme_sector_from_history(
            name=name,
            value=value,
            label=label,
            note=note,
            source=source,
            history=history,
            detail_type=detail_type,
        )
        if detail_type == "policy":
            item.detail = policy_note
        item.updated_at = now_label
        sectors.append(item)
    if market_theme_sectors:
        sectors.extend(market_theme_sectors)
    existing_names = {item.name for item in sectors}
    for name, _code, display_name in THEME_MARKET_INDEXES:
        if name in existing_names:
            continue
        sectors.append(ThemeSectorInfo(
            name=name,
            value=None,
            label="待更新",
            note=display_name,
            tone="neutral",
            source="主题指数",
            updated_at=now_label,
        ))
    return sectors


async def enhance_funds_with_deepseek(
    funds: List[FundInfo],
    index_info: IndexInfo,
    bond_index: Optional[IndexInfo],
    news: List[NewsItem],
    strict: bool = False,
) -> List[FundInfo]:
    """用 DeepSeek 增强策略分析。自动场景可回退，手动场景必须明确成功或报错。"""
    api_key = get_deepseek_api_key()
    if not api_key or not funds:
        if strict:
            raise HTTPException(status_code=400, detail="DeepSeek 未配置或暂无基金数据")
        return funds

    def _first_number(*values, default=0.0):
        for value in values:
            try:
                if value is not None:
                    return float(value)
            except Exception:
                continue
        return default

    payload = {
        "market": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "sh_index": {"value": index_info.current, "change": index_info.daily_change},
            "bond_index": {"value": bond_index.current if bond_index else 0, "change": bond_index.daily_change if bond_index else 0},
        },
        "news": [
            {
                "title": n.title,
                "time": n.time,
                "event_date": n.event_date,
                "importance": n.importance,
                "tags": n.tags,
            }
            for n in (news or [])[:12]
        ],
        "funds": [
            {
                "code": f.code,
                "name": f.name,
                "type": f.type,
                "nav_date": f.nav_date,
                "daily_change": f.daily_change,
                "estimated_change": _first_number(
                    f.corrected_estimated_change,
                    f.model_estimated_change,
                    f.estimated_change,
                    f.ai_prediction.est_change if f.ai_prediction else None,
                ),
                "return_7d": f.return_7d,
                "return_1m": f.return_1m,
                "return_6m": f.return_6m,
                "buy_point": {
                    "is_holding": bool(f.buy_point and f.buy_point.is_holding),
                    "yield_pct": f.buy_point.yield_pct if f.buy_point else 0,
                    "drop_pct": f.buy_point.drop_pct if f.buy_point else 0,
                    "progress_pct": f.buy_point.progress_pct if f.buy_point else 0,
                },
                "local_ai": {
                    "trend": f.ai_prediction.trend if f.ai_prediction else "",
                    "advice": f.ai_prediction.advice if f.ai_prediction else "",
                    "risk_level": f.ai_prediction.risk_level if f.ai_prediction else "",
                    "key_levels": f.ai_prediction.key_levels if f.ai_prediction else "",
                },
            }
            for f in funds
        ],
    }
    prompt = (
        "你是基金组合策略分析助手。请基于给定JSON生成更有用的中文策略分析。"
        "只返回JSON对象，格式：{\"funds\":{\"基金代码\":{\"advice\":\"一句话核心结论，必须和本地规则不同，不超过42字\","
        "\"market_env\":\"市场/新闻影响，不超过60字\", \"position_advice\":\"当前动作建议，只能是观察/持有/等待买点/谨慎补仓/止盈观察之一，并补一句理由\", "
        "\"risk_tips\":\"最主要风险，不超过50字\", \"risk_level\":\"低/中/高\", \"trend\":\"趋势标签，不超过6字\"}}}。"
        "必须结合预估收益、买点距离、持仓状态、未来一周重要事件。不要给金额建议，不要夸大确定性。"
    )
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.post(
                DEEPSEEK_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:].strip()
            data = json.loads(content)
    except Exception as e:
        print(f"DeepSeek 分析失败，使用本地规则: {e}")
        if strict:
            raise HTTPException(status_code=502, detail=f"DeepSeek 分析失败：{e}")
        return funds

    ai_map = data.get("funds", {}) if isinstance(data, dict) else {}
    if strict and not ai_map:
        raise HTTPException(status_code=502, detail="DeepSeek 未返回有效基金分析")

    normalized_ai_map = {}
    if isinstance(ai_map, dict):
        for key, val in ai_map.items():
            code = re.sub(r"\D", "", str(key))[-6:]
            if code:
                normalized_ai_map[code] = val

    changed_count = 0
    for f in funds:
        item = normalized_ai_map.get(f.code) or ai_map.get(f.code) or {}
        if not item or not f.ai_prediction:
            continue
        before = (
            f.ai_prediction.advice,
            f.ai_prediction.market_env,
            f.ai_prediction.position_advice,
            f.ai_prediction.risk_tips,
            f.ai_prediction.risk_level,
            f.ai_prediction.trend,
        )
        f.ai_prediction.advice = str(item.get("advice") or f.ai_prediction.advice)[:120]
        f.ai_prediction.market_env = str(item.get("market_env") or f.ai_prediction.market_env)[:240]
        f.ai_prediction.position_advice = str(item.get("position_advice") or f.ai_prediction.position_advice)[:240]
        f.ai_prediction.risk_tips = str(item.get("risk_tips") or f.ai_prediction.risk_tips)[:240]
        f.ai_prediction.confidence = "DeepSeek"
        if item.get("risk_level") in ["低", "中", "高"]:
            f.ai_prediction.risk_level = item["risk_level"]
        if item.get("trend"):
            f.ai_prediction.trend = str(item["trend"])[:12]
        after = (
            f.ai_prediction.advice,
            f.ai_prediction.market_env,
            f.ai_prediction.position_advice,
            f.ai_prediction.risk_tips,
            f.ai_prediction.risk_level,
            f.ai_prediction.trend,
        )
        if after != before:
            changed_count += 1

    if strict and changed_count == 0:
        raise HTTPException(status_code=502, detail="DeepSeek 已返回，但没有产生可展示的新分析")
    return funds


def _parse_tencent_kline(text: str, stock_code: str = "sh000001") -> List[tuple]:
    """
    解析腾讯 K 线返回文本，提取每日原始数据
    返回: [(date_str, close, previous_close), ...] 按日期升序（从旧到新）
    """
    prefix = "kline_dayqfq="
    idx = text.find(prefix)
    if idx < 0:
        return []
    json_str = text[idx + len(prefix):].rstrip(';').strip()
    try:
        data = json.loads(json_str)
    except Exception:
        return []

    days_data = data.get('data', {}).get(stock_code, {}).get('day', [])
    if not days_data:
        return []

    result = []
    for d in days_data:
        # 腾讯 day K 线字段: [date, open, close, high, low, volume, amount, ...]
        # 不同版本字段顺序略有差异，但都是 [日期字符串, 多个数值]
        try:
            date_str = str(d[0])
            close = float(d[2])
            prev_close = float(d[1])  # 这里 d[1] 是 open，不是昨收；后面会用前一天的 close 来计算
            result.append((date_str, close, prev_close))
        except (ValueError, IndexError, TypeError):
            continue
    return result


async def _fetch_tencent_kline_for_code(stock_code: str, request_days: int = 30) -> List[tuple]:
    """
    从腾讯 K 线接口拉取指定指数最近 N 根日 K 线
    返回按日期升序（从旧到新）的 list[(date, close, open)]
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={stock_code},day,,,{request_days},qfq&r=0.1"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers=HTTP_HEADERS)
            r.raise_for_status()
            text = r.text
        return _parse_tencent_kline(text, stock_code)
    except Exception as e:
        print(f"获取 {stock_code} K 线失败: {e}")
        return []


async def _fetch_tencent_kline(request_days: int = 30) -> List[tuple]:
    """
    从腾讯 K 线接口拉取上证指数最近 N 根日 K 线
    返回按日期升序（从旧到新）的 list[(date, close, open)]
    """
    return await _fetch_tencent_kline_for_code("sh000001", request_days)


async def fetch_generic_index(code: str, name: str) -> IndexInfo:
    """
    通用指数抓取（用腾讯K线接口）
    """
    rt = await fetch_eastmoney_realtime_index(code, name)
    if not (rt and rt.get("current", 0) > 0):
        rt = await fetch_realtime_index(code)
    if rt and rt.get("current", 0) > 0:
        return IndexInfo(
            code=code.replace("sh", "").replace("sz", ""),
            name=name,
            current=round(float(rt.get("current", 0)), 2),
            previous=round(float(rt.get("previous", 0)), 2),
            daily_change=round(float(rt.get("change_pct", 0)), 2),
            history=[]
        )

    raw = await _fetch_tencent_kline_for_code(code, request_days=30)
    if not raw or len(raw) == 0:
        return IndexInfo(
            code=code.replace("sh", "").replace("sz", ""),
            name=name,
            current=0.0,
            previous=0.0,
            daily_change=0.0,
            history=[]
        )

    latest = raw[-1]
    latest_date, latest_close = latest[0], latest[1]
    if len(raw) >= 2:
        previous_close = raw[-2][1]
    else:
        previous_close = latest[2]

    daily_change = round((latest_close - previous_close) / previous_close * 100, 2) if previous_close > 0 else 0.0
    display_code = code.replace("sh", "").replace("sz", "")

    return IndexInfo(
        code=display_code,
        name=name,
        current=round(latest_close, 2),
        previous=round(previous_close, 2),
        daily_change=daily_change
    )


async def fetch_yahoo_index(symbol: str, name: str) -> Optional[IndexInfo]:
    """Fetch global index quote from Yahoo chart API for external-market temperature."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        params = {"range": "5d", "interval": "1d"}
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=HTTP_HEADERS)
            r.raise_for_status()
            payload = r.json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        quote_data = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
        closes = [float(x) for x in (quote_data.get("close") or []) if x is not None and float(x) > 0]
        current = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0))
        previous = float(meta.get("chartPreviousClose") or (closes[-2] if len(closes) >= 2 else 0))
        if current <= 0:
            return None
        if previous <= 0:
            previous = current
        daily_change = round((current - previous) / previous * 100, 2) if previous > 0 else 0.0
        return IndexInfo(
            code=symbol,
            name=name,
            current=round(current, 2),
            previous=round(previous, 2),
            daily_change=daily_change,
            history=[]
        )
    except Exception as e:
        print(f"[外部市场] {symbol} 获取失败: {e}")
        return None


async def fetch_sina_us_stock(symbol: str, name: str) -> Optional[IndexInfo]:
    """Domestic-accessible Sina quote for US stocks: gb_nvda, gb_amd, etc."""
    try:
        clean = re.sub(r"[^A-Za-z]", "", symbol or "").lower()
        if not clean:
            return None
        url = "https://hq.sinajs.cn/list=gb_" + clean
        headers = {
            **HTTP_HEADERS,
            "Referer": "https://finance.sina.com.cn/stock/usstock/",
        }
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.content.decode("gbk", errors="ignore").strip()
        m = re.search(r'="([^"]*)"', text)
        if not m:
            return None
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) < 3 or not parts[1]:
            return None
        current = float(parts[1])
        change_pct = float(parts[2])
        previous = current / (1 + change_pct / 100) if change_pct != -100 else current
        if current <= 0:
            return None
        return IndexInfo(
            code=symbol.upper(),
            name=name or parts[0] or symbol.upper(),
            current=round(current, 2),
            previous=round(previous, 2),
            daily_change=round(change_pct, 2),
            history=[]
        )
    except Exception as e:
        print(f"[外部市场-新浪美股] {symbol} 获取失败: {e}")
        return None


async def fetch_eastmoney_global_index(secid: str, name: str) -> Optional[IndexInfo]:
    """Eastmoney global index quote, using the exact global secid."""
    if not secid:
        return None
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f43,f57,f58,f60,f169,f170",
            "_": int(time.time() * 1000),
        }
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers={**HTTP_HEADERS, "Referer": "https://quote.eastmoney.com/center/gridlist.html"})
            r.raise_for_status()
            data = r.json().get("data") or {}
        current = _em_scaled(data.get("f43"))
        previous = _em_scaled(data.get("f60"))
        change_amt = _em_scaled(data.get("f169"))
        change_pct = _em_scaled(data.get("f170"))
        if current <= 0:
            return None
        return IndexInfo(
            code=secid,
            name=name or data.get("f58") or secid,
            current=round(current, 2),
            previous=round(previous, 2),
            daily_change=round(change_pct, 2),
            history=[]
        )
    except Exception as e:
        print(f"[外部市场-东财] {secid} 获取失败: {e}")
        return None


async def fetch_stooq_index(symbol: str, name: str) -> Optional[IndexInfo]:
    """Fallback global index quote from Stooq daily CSV."""
    try:
        end = datetime.now()
        start = end - timedelta(days=18)
        url = "https://stooq.com/q/d/l/"
        params = {
            "s": symbol,
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
        }
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=HTTP_HEADERS)
            r.raise_for_status()
            text = r.text.strip()
        rows = []
        for line in text.splitlines()[1:]:
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 5 and parts[4] not in ("", "0", "N/D"):
                rows.append(parts)
        if not rows:
            return None
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else rows[-1]
        current = float(latest[4])
        prev_close = float(previous[4])
        if current <= 0 or prev_close <= 0:
            return None
        daily_change = round((current - prev_close) / prev_close * 100, 2)
        return IndexInfo(
            code=symbol,
            name=name,
            current=round(current, 2),
            previous=round(prev_close, 2),
            daily_change=daily_change,
            history=[]
        )
    except Exception as e:
        print(f"[外部市场-Stooq] {symbol} 获取失败: {e}")
        return None


async def fetch_korea_quote(secid: str, yahoo_symbol: str, stooq_symbol: str, name: str) -> Optional[IndexInfo]:
    """Korea market quote via domestic Eastmoney sources only."""
    raw = (secid or "").strip()
    candidates = []
    for value in (raw, raw.lower(), raw.upper()):
        if value and value not in candidates:
            candidates.append(value)
    for candidate in candidates:
        item = await fetch_eastmoney_global_index(candidate, name)
        if item is not None:
            return item
    return None


async def fetch_external_index(em_secid: str, yahoo_symbol: str, stooq_symbol: str, name: str) -> Optional[IndexInfo]:
    """Domestic sources only: Eastmoney exact secid first, Sina US quote second."""
    item = await fetch_eastmoney_global_index(em_secid, name)
    if item is not None:
        return item
    item = await fetch_sina_us_stock(yahoo_symbol, name)
    if item is not None:
        return item
    return None


def _external_tone(value: Optional[float]) -> str:
    if value is None:
        return "neutral"
    if value > 0.15:
        return "good"
    if value < -0.15:
        return "bad"
    return "neutral"


async def fetch_external_market_temperature() -> List[ThemeSectorInfo]:
    """Fetch US/Korea markets and return cards using the same temperature model."""
    now_text = datetime.now().strftime("%H:%M")
    # us-stock-groups-cn-source-20260730: external market cards use domestic-accessible
    # quotes for US stock groups only; Korea cards keep the existing shape.
    specs = [
        ("us", "100.NDX", "^NDX", "^ndx", "纳指100"),
        ("us", "100.DJIA", "^DJI", "^dji", "道琼斯"),
        ("us", "251.SOX", "^SOX", "^sox", "费城半导体"),
        ("kr", "100.KS11", "^KS11", "^ks11", "韩国KOSPI"),
        ("kr", "177.005930", "005930.KS", "005930.kr", "三星电子"),
        ("kr", "177.000660", "000660.KS", "000660.kr", "SK海力士"),
        ("us", "105.NVDA", "NVDA", "nvda.us", "英伟达"),
        ("us", "105.AMD", "AMD", "amd.us", "AMD"),
        ("us", "105.AVGO", "AVGO", "avgo.us", "博通"),
        ("us", "105.MSFT", "MSFT", "msft.us", "微软"),
        ("us", "105.AAPL", "AAPL", "aapl.us", "苹果"),
        ("us", "105.GOOGL", "GOOGL", "googl.us", "谷歌"),
        ("us", "105.AMZN", "AMZN", "amzn.us", "亚马逊"),
        ("us", "105.META", "META", "meta.us", "Meta"),
        ("us", "105.TSLA", "TSLA", "tsla.us", "特斯拉"),
        ("us", "105.TSM", "TSM", "tsm.us", "台积电"),
        ("us", "105.ASML", "ASML", "asml.us", "阿斯麦"),
        ("us", "105.AMAT", "AMAT", "amat.us", "应用材料"),
        ("us", "105.ORCL", "ORCL", "orcl.us", "甲骨文"),
        ("us", "105.CRM", "CRM", "crm.us", "赛富时"),
        ("us", "105.ADBE", "ADBE", "adbe.us", "Adobe"),
        ("us", "105.NOW", "NOW", "now.us", "ServiceNow"),
        ("us", "105.MU", "MU", "mu.us", "美光"),
        ("us", "105.WDC", "WDC", "wdc.us", "西部数据"),
        ("us", "105.SMCI", "SMCI", "smci.us", "超微电脑"),
        ("us", "105.DELL", "DELL", "dell.us", "戴尔"),
        ("us", "105.COHR", "COHR", "cohr.us", "Coherent"),
        ("us", "105.LITE", "LITE", "lite.us", "Lumentum"),
        ("us", "105.ON", "ON", "on.us", "安森美"),
        ("us", "105.TLT", "TLT", "tlt.us", "美债ETF"),
        ("us", "105.UUP", "UUP", "uup.us", "美元指数ETF"),
        ("us", "105.KO", "KO", "ko.us", "可口可乐"),
        ("us", "105.PG", "PG", "pg.us", "宝洁"),
        ("us", "105.WMT", "WMT", "wmt.us", "沃尔玛"),
        ("us", "105.MCD", "MCD", "mcd.us", "麦当劳"),
        ("us", "105.COST", "COST", "cost.us", "Costco"),
        ("us", "105.JPM", "JPM", "jpm.us", "摩根大通"),
        ("us", "105.BAC", "BAC", "bac.us", "美国银行"),
        ("us", "105.GS", "GS", "gs.us", "高盛"),
        ("us", "105.BRK.B", "BRK.B", "brkb.us", "伯克希尔"),
        ("us", "105.BLK", "BLK", "blk.us", "黑石"),
        ("us", "105.XOM", "XOM", "xom.us", "埃克森美孚"),
        ("us", "105.CVX", "CVX", "cvx.us", "雪佛龙"),
        ("us", "105.COP", "COP", "cop.us", "康菲石油"),
        ("us", "105.SLB", "SLB", "slb.us", "斯伦贝谢"),
        ("us", "105.JNJ", "JNJ", "jnj.us", "强生"),
        ("us", "105.LLY", "LLY", "lly.us", "礼来"),
        ("us", "105.UNH", "UNH", "unh.us", "联合健康"),
        ("us", "105.MRK", "MRK", "mrk.us", "默沙东"),
        ("us", "105.PFE", "PFE", "pfe.us", "辉瑞"),
        ("us", "105.CAT", "CAT", "cat.us", "卡特彼勒"),
        ("us", "105.GE", "GE", "ge.us", "通用电气"),
        ("us", "105.BA", "BA", "ba.us", "波音"),
        ("us", "105.HON", "HON", "hon.us", "霍尼韦尔"),
        ("us", "105.LMT", "LMT", "lmt.us", "洛克希德马丁"),
        ("us", "105.VZ", "VZ", "vz.us", "Verizon"),
        ("us", "105.T", "T", "t.us", "AT&T"),
        ("us", "105.NEE", "NEE", "nee.us", "NextEra"),
        ("us", "105.DUK", "DUK", "duk.us", "杜克能源"),
        ("us", "105.O", "O", "o.us", "Realty Income"),
    ]
    results = await asyncio.gather(
        *(
            fetch_korea_quote(em_secid, yahoo_symbol, stooq_symbol, name) if market == "kr"
            else fetch_external_index(em_secid, yahoo_symbol, stooq_symbol, name)
            for market, em_secid, yahoo_symbol, stooq_symbol, name in specs
        ),
        return_exceptions=True
    )
    quotes: Dict[str, IndexInfo] = {}
    for (_market, _em_secid, _yahoo_symbol, _stooq_symbol, name), result in zip(specs, results):
        if isinstance(result, IndexInfo):
            quotes[name] = result
        elif isinstance(result, Exception):
            print(f"[外部市场] {name} 异常: {result}")

    def avg(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    def q(name: str) -> Optional[float]:
        item = quotes.get(name)
        return item.daily_change if item else None

    def basket(names: List[str]) -> Optional[float]:
        return avg([x for x in [q(name) for name in names] if x is not None])

    us_market = avg([x for x in [q("纳指100"), q("道琼斯")] if x is not None])
    # external-us-tech-focus-20260730: keep card count/layout, but make US cards
    # reflect the AI/semiconductor chain instead of broad sector baskets.
    us_tech = basket(["微软", "苹果"])
    us_ai_chip = basket(["英伟达", "博通"])
    if us_ai_chip is None:
        us_ai_chip = q("费城半导体")
    us_cloud = basket(["甲骨文", "Adobe"])
    us_chip_equipment = basket(["阿斯麦", "应用材料"])
    us_memory = basket(["美光", "西部数据"])
    us_compute_server = basket(["超微电脑", "戴尔"])
    us_optical = basket(["Coherent", "Lumentum"])
    us_ev_chain = basket(["特斯拉", "安森美"])
    rate_parts = []
    tlt = q("美债ETF")
    uup = q("美元指数ETF")
    if tlt is not None:
        rate_parts.append(-tlt)
    if uup is not None:
        rate_parts.append(uup)
    us_rate_pressure = avg(rate_parts)
    korea_chip_stock = avg([x for x in [q("三星电子"), q("SK海力士")] if x is not None])
    korea_chip = korea_chip_stock if korea_chip_stock is not None else avg([x for x in [q("韩国KOSPI"), q("费城半导体")] if x is not None])
    korea_market = korea_chip_stock if korea_chip_stock is not None else q("韩国KOSPI")
    korea_market_note = "三星电子 / SK海力士"

    cards = [
        ("美股大盘", us_market, "纳指100 / 道指"),
        ("AI半导体", us_ai_chip, "英伟达 / 博通"),
        ("科技巨头", us_tech, "微软 / 苹果"),
        ("云计算软件", us_cloud, "甲骨文 / Adobe"),
        ("芯片设备", us_chip_equipment, "ASML / 应用材料"),
        ("存储芯片", us_memory, "美光 / 西部数据"),
        ("算力服务器", us_compute_server, "超微电脑 / 戴尔"),
        ("光通信链", us_optical, "Coherent / Lumentum"),
        ("电动车链", us_ev_chain, "特斯拉 / 安森美"),
        ("利率压力", us_rate_pressure, "美债 / 美元"),
        ("韩国股市", korea_market, korea_market_note),
        ("韩国半导体", korea_chip, "三星电子 / SK海力士"),
    ]
    return [
        ThemeSectorInfo(
            name=name,
            value=round(value, 2) if value is not None else None,
            label="" if value is not None else "待更新",
            note=note,
            tone=_external_tone(value),
            source="外部市场",
            updated_at=now_text,
        )
        for name, value, note in cards
    ]


async def get_external_market_temperature() -> List[ThemeSectorInfo]:
    """Backend-managed external market cache. Frontend reads the cached result only."""
    now_ts = time.time()
    cached = EXTERNAL_MARKET_CACHE.get("data")
    if cached is not None and (now_ts - EXTERNAL_MARKET_CACHE.get("saved_at", 0.0)) < EXTERNAL_MARKET_CACHE_TTL:
        return cached
    fresh = await fetch_external_market_temperature()
    has_value = any(getattr(item, "value", None) is not None for item in fresh)
    if has_value or cached is None:
        EXTERNAL_MARKET_CACHE["data"] = fresh
        EXTERNAL_MARKET_CACHE["saved_at"] = now_ts
        return fresh
    # Keep the last usable snapshot when all upstreams fail temporarily.
    return cached


async def fetch_index_history_for_code(code: str, days: int = 7) -> List[IndexHistoryItem]:
    """
    获取指定指数近 N 天历史 K 线
    """
    raw = await _fetch_tencent_kline_for_code(code, request_days=days + 15)
    if not raw:
        return []

    recent = raw[-days:] if len(raw) >= days else raw
    start_idx = len(raw) - len(recent)

    result = []
    for i, item in enumerate(recent):
        date_str, close, _ = item
        raw_idx = start_idx + i
        if raw_idx > 0:
            previous_close = raw[raw_idx - 1][1]
        else:
            previous_close = item[2]
        change = round((close - previous_close) / previous_close * 100, 2) if previous_close > 0 else 0.0
        result.append(IndexHistoryItem(
            date=date_str,
            close=round(close, 2),
            change=change
        ))

    result.reverse()
    return result


async def fetch_sh_index() -> IndexInfo:
    """
    获取上证指数：基于腾讯 K 线（和基金净值接口同逻辑），
    取"最近一个已收盘的交易日"作为当前数据，保证非交易时间也能拿到最新一条。
    """
    raw = await _fetch_tencent_kline(request_days=30)
    if not raw or len(raw) == 0:
        return IndexInfo(
            code="000001",
            name="上证指数",
            current=0.0,
            previous=0.0,
            daily_change=0.0,
            history=[]
        )

    # 最近一根 = 最新交易日收盘
    latest = raw[-1]
    latest_date, latest_close = latest[0], latest[1]

    # 昨收 = 前一根 K 线的 close（没有就用当天 open 作为兜底）
    if len(raw) >= 2:
        previous_close = raw[-2][1]
    else:
        previous_close = latest[2]

    daily_change = round((latest_close - previous_close) / previous_close * 100, 2) if previous_close > 0 else 0.0

    return IndexInfo(
        code="000001",
        name="上证指数",
        current=round(latest_close, 2),
        previous=round(previous_close, 2),
        daily_change=daily_change
    )


async def fetch_index_history(days: int = 7) -> List[IndexHistoryItem]:
    """
    获取上证指数近 N 天历史 K 线
    返回按日期 **倒序**（最新交易日排第一），保证前端展示最新在最上面
    """
    raw = await _fetch_tencent_kline(request_days=days + 15)
    if not raw:
        return []

    # raw 按日期升序（从旧到新）。取末尾 days 根 = 最近 days 个交易日。
    recent = raw[-days:] if len(raw) >= days else raw
    start_idx = len(raw) - len(recent)

    # 为每根计算相对昨收的日涨跌（"昨收"=前一根 K 线的 close）
    result = []
    for i, item in enumerate(recent):
        date_str, close, _ = item
        raw_idx = start_idx + i
        if raw_idx > 0:
            previous_close = raw[raw_idx - 1][1]
        else:
            previous_close = item[2]  # 没有前一根，用当天 open 兜底
        change = round((close - previous_close) / previous_close * 100, 2) if previous_close > 0 else 0.0
        result.append(IndexHistoryItem(
            date=date_str,
            close=round(close, 2),
            change=change
        ))

    # 按日期倒序返回（最新在最前）
    result.reverse()
    return result


def normalize_index_history(history: Optional[List[IndexHistoryItem]], limit: Optional[int] = None) -> List[IndexHistoryItem]:
    """Return stable index history: one row per date, ascending, latest kept on duplicate dates."""
    by_date: dict[str, IndexHistoryItem] = {}
    for item in history or []:
        if not item or not getattr(item, "date", ""):
            continue
        try:
            close = float(getattr(item, "close", 0) or 0)
            change = float(getattr(item, "change", 0) or 0)
        except Exception:
            continue
        if close <= 0:
            continue
        by_date[str(item.date)[:10]] = IndexHistoryItem(
            date=str(item.date)[:10],
            close=round(close, 2),
            change=round(change, 2),
        )
    rows = [by_date[date] for date in sorted(by_date)]
    if limit and limit > 0:
        rows = rows[-limit:]
    return rows


def normalize_index_info_history(index_info: Optional[IndexInfo], limit: Optional[int] = None) -> Optional[IndexInfo]:
    if index_info is None:
        return None
    index_info.history = normalize_index_history(getattr(index_info, "history", None), limit=limit)
    return index_info


def home_index_visual_history(index_info: Optional[IndexInfo], limit: int = 12) -> List[IndexHistoryItem]:
    """Stable history for the home Shanghai sparkline.

    The previous synthetic version reshaped historical closes around the
    current point. That made the sparkline visibly change on refresh even when
    the displayed index value barely moved. Keep the real normalized closes and
    let the frontend append today's live point when needed.
    """
    if index_info is None:
        return []
    return normalize_index_history(getattr(index_info, "history", None), limit=limit)


def portfolio_response_for_client(response: PortfolioResponse, lite: int = 0) -> PortfolioResponse:
    """Return a client copy; keep cached/raw response untouched."""
    if lite:
        return lite_portfolio_response(response)
    client = response.copy(deep=True)
    if client.index:
        client.index.history = home_index_visual_history(client.index, limit=12)
    return client


# ============= API 接口 =============

def lite_portfolio_response(response: PortfolioResponse) -> PortfolioResponse:
    """首页首包瘦身：只保留首页必要数据，市场/资讯/弹窗详情进入页面后再加载。"""
    lite_response = response.copy(deep=True)
    lite_response.news = []
    lite_response.theme_sectors = []
    lite_response.external_markets = []
    for attr in ("index", "bond_index", "k50_index", "hsi_index", "hs300_index", "sz_index"):
        item = getattr(lite_response, attr, None)
        if item:
            item.history = normalize_index_history(getattr(item, "history", None), limit=12)
            if attr == "index":
                item.history = home_index_visual_history(item, limit=12)
    if getattr(lite_response, "funds", None):
        for fund in lite_response.funds:
            fund.history = []
            fund.holdings = []
            if fund.buy_point and getattr(fund.buy_point, "yield_history", None):
                fund.buy_point.yield_history = []
    return lite_response


@app.get("/api/portfolio", response_model=PortfolioResponse)
async def get_portfolio(force: int = 0, lite: int = 0):
    """
    获取持仓概览（包括上证指数、国债指数、关注基金含7天净值、买点判断、AI预判）
    force=1 强制刷新（绕过 30s 整页缓存）
    lite=1 首页首包轻量模式：市场/资讯/弹窗数据懒加载
    """
    # === 整页缓存：业务状态判定（去 TTL 概念） ===
    # 用户场景：日涨跌没更新前一直拉，更新到了才停
    # 规则：
    # - 已披露（cache 中所有基金 nav_date == 最新可披露日期）→ 缓存命中（force=0 命中，force=1 也命中）
    # - 未披露（仍有基金 nav_date < 最新可披露日期）→ 走 fetch（继续拉，直到披露）
    # - 盘中未披露时：30s 轮询 force=0 也走 fetch（要拿盘中估值）；force=1 也走 fetch
    # 注：盘外 30s 轮询若已披露则命中缓存
    now_ts = time.time()
    now = datetime.now()
    is_trading = is_trading_time()
    disclosed = _is_today_disclosed()
    if force == 0:
        max_age = portfolio_snapshot_max_age_seconds()
        if PORTFOLIO_CACHE["data"] is not None:
            cached_response = PORTFOLIO_CACHE["data"].copy(deep=True)
            cached_response.time = now.strftime("%H:%M:%S")
            if now_ts - PORTFOLIO_CACHE.get("saved_at", 0.0) > max_age:
                schedule_portfolio_refresh("memory_snapshot_stale")
            return portfolio_response_for_client(cached_response, lite)

        db_response, db_age = load_portfolio_snapshot_from_db()
        if db_response is not None:
            db_response.time = now.strftime("%H:%M:%S")
            PORTFOLIO_CACHE["data"] = db_response
            PORTFOLIO_CACHE["saved_at"] = now_ts - float(db_age or 0)
            if db_age is None or db_age > max_age:
                schedule_portfolio_refresh("sqlite_snapshot_stale")
            return portfolio_response_for_client(db_response, lite)

    if force == 0 and PORTFOLIO_CACHE["data"] is None:
        db_max_age = 180 if is_trading else 900
        db_response = load_portfolio_from_db(max_age_seconds=db_max_age)
        if db_response is not None:
            db_response.time = now.strftime("%H:%M:%S")
            PORTFOLIO_CACHE["data"] = db_response
            PORTFOLIO_CACHE["saved_at"] = now_ts
            return portfolio_response_for_client(db_response, lite)
    if PORTFOLIO_CACHE["data"] is not None:
        if disclosed and force == 0:
            # 日涨跌已披露（当日 15:00 后 / 周末 = 上周五已披露）→ 缓存命中即可
            cached_response = PORTFOLIO_CACHE["data"].copy(deep=True)
            if lite:
                cached_response.time = now.strftime("%H:%M:%S")
                return portfolio_response_for_client(cached_response, lite)
            news_age = now_ts - NEWS_CACHE.get("saved_at", 0.0)
            if NEWS_CACHE["data"] is None or news_age >= NEWS_LIST_CACHE_TTL:
                schedule_news_refresh()
            cached_response.news = current_news_or_placeholder()
            cached_market_themes = [x for x in (cached_response.theme_sectors or []) if getattr(x, "source", "") == "主题指数"]
            cached_response.theme_sectors = build_theme_sectors(
                cached_response.funds,
                cached_response.index,
                cached_response.bond_index,
                cached_response.hs300_index,
                cached_response.sz_index,
                cached_response.news,
                cached_market_themes,
                cached_response.k50_index,
            )
            cached_response.external_markets = await get_external_market_temperature()
            cached_response.time = now.strftime("%H:%M:%S")
            return portfolio_response_for_client(cached_response, lite)
        # 未披露：盘中 30s 内 force=0 命中（盘中估值微动不必要求 30s 一拉）
        if force == 0 and is_trading and (now_ts - PORTFOLIO_CACHE["saved_at"]) < PORTFOLIO_CACHE_TTL:
            cached_response = PORTFOLIO_CACHE["data"].copy(deep=True)
            cached_response.time = now.strftime("%H:%M:%S")
            return portfolio_response_for_client(cached_response, lite)

    # 并行获取市场指数：上证指数 + 上证历史 + 科创50 + 恒生指数 + 国债指数 + 沪深300 + 深证成指 + 上证50
    index_task = fetch_sh_index()
    index_history_task = asyncio.sleep(0, result=load_index_history_from_db("shanghai", 12)) if lite else fetch_index_history(190)
    k50_task = fetch_generic_index("sh000688", "科创50")
    k50_history_task = asyncio.sleep(0, result=[]) if lite else fetch_index_history_for_code("sh000688", 190)
    hsi_task = fetch_generic_index("hkHSI", "恒生指数")
    bond_index_task = fetch_bond_market_proxy_index()
    bond_history_task = asyncio.sleep(0, result=[]) if lite else fetch_index_history_for_code("sh000012", 190)
    hs300_task = fetch_generic_index("sh000300", "沪深300")
    hs300_history_task = asyncio.sleep(0, result=[]) if lite else fetch_index_history_for_code("sh000300", 190)
    sz_index_task = fetch_generic_index("sz399001", "深证指数")
    sz_history_task = asyncio.sleep(0, result=[]) if lite else fetch_index_history_for_code("sz399001", 190)
    sh50_task = fetch_generic_index("sh000016", "上证50")
    sh50_history_task = asyncio.sleep(0, result=[]) if lite else fetch_index_history_for_code("sh000016", 190)

    (
        index_base, index_history, k50_base, k50_history, hsi_base, bond_index_base, bond_history,
        hs300_base, hs300_history, sz_index_base, sz_history, sh50_base, sh50_history
    ) = await asyncio.gather(
        index_task, index_history_task, k50_task, k50_history_task, hsi_task, bond_index_task, bond_history_task,
        hs300_task, hs300_history_task, sz_index_task, sz_history_task, sh50_task, sh50_history_task
    )
    if lite and len(index_history or []) < 2:
        index_history = await fetch_index_history(12)

    # 构建完整指数数据
    index_info = IndexInfo(
        code=index_base.code,
        name=index_base.name,
        current=index_base.current,
        previous=index_base.previous,
        daily_change=index_base.daily_change,
        history=index_history
    )

    bond_index_info = IndexInfo(
        code=bond_index_base.code,
        name=bond_index_base.name,
        current=bond_index_base.current,
        previous=bond_index_base.previous,
        daily_change=bond_index_base.daily_change,
        history=bond_history
    )

    k50_index_info = IndexInfo(
        code=k50_base.code,
        name=k50_base.name,
        current=k50_base.current,
        previous=k50_base.previous,
        daily_change=k50_base.daily_change,
        history=k50_history
    )

    hsi_index_info = IndexInfo(
        code=hsi_base.code,
        name=hsi_base.name,
        current=hsi_base.current,
        previous=hsi_base.previous,
        daily_change=hsi_base.daily_change,
        history=[]
    )

    # 构建沪深300指数数据
    hs300_info = IndexInfo(
        code=hs300_base.code,
        name=hs300_base.name,
        current=hs300_base.current,
        previous=hs300_base.previous,
        daily_change=hs300_base.daily_change,
        history=hs300_history
    )

    sz_index_info = IndexInfo(
        code=sz_index_base.code,
        name=sz_index_base.name,
        current=sz_index_base.current,
        previous=sz_index_base.previous,
        daily_change=sz_index_base.daily_change,
        history=sz_history
    )

    sh50_info = IndexInfo(
        code=sh50_base.code,
        name=sh50_base.name,
        current=sh50_base.current,
        previous=sh50_base.previous,
        daily_change=sh50_base.daily_change,
        history=sh50_history
    )

    # 并行获取所有基金数据（含实时净值 + 历史净值 + AI预判）
    # force=1 时透传给 fetch_fund_from_eastmoney → period_returns / history 绕过 60s 子缓存
    fund_tasks = [fetch_fund_from_eastmoney(code, stock_index=index_info, bond_index=bond_index_info, hs300_index=hs300_info, force=force, lite=lite) for code in WATCHED_FUNDS]
    if not lite:
        theme_market_task = fetch_theme_market_sectors()
        external_market_task = get_external_market_temperature()
    fund_results = await asyncio.gather(*fund_tasks, return_exceptions=True)
    if lite:
        theme_market_sectors, external_markets = [], []
    else:
        theme_market_sectors, external_markets = await asyncio.gather(theme_market_task, external_market_task)

    funds: List[FundInfo] = []
    for result in fund_results:
        if isinstance(result, FundInfo):
            funds.append(result)
        elif isinstance(result, Exception):
            print(f"基金获取异常: {result}")

    news_age = now_ts - NEWS_CACHE.get("saved_at", 0.0)
    if not lite and (NEWS_CACHE["data"] is None or news_age >= NEWS_LIST_CACHE_TTL or force):
        schedule_news_refresh(force=bool(force))
    news = [] if lite else current_news_or_placeholder()
    theme_sectors = [] if lite else build_theme_sectors(
            funds,
            index_info,
            bond_index_info,
            hs300_info,
            sz_index_info,
            news,
            theme_market_sectors,
            k50_index_info,
            sh50_info,
        )

    for idx in (index_info, bond_index_info, k50_index_info, hsi_index_info, hs300_info, sz_index_info):
        normalize_index_info_history(idx, limit=190)

    # 新闻后台刷新，不阻塞首页首屏
    # ❗ 注意：此处不保存 buy_points.json，该文件仅由 /api/buy 和 /api/sell 接口写入
    # 未确认买入的基金，其虚拟成本只在内存中计算，不持久化

    response = PortfolioResponse(
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
        is_trading_day=is_trading_day(now),
        display_trade_date=_display_trade_date(now),
        latest_disclosed_date=_latest_disclosed_date(),
        market_status=market_status(),
        index=index_info,
        funds=funds,
        news=news,
        bond_index=bond_index_info,
        k50_index=k50_index_info,
        hsi_index=hsi_index_info,
        hs300_index=hs300_info,
        sz_index=sz_index_info,
        theme_sectors=theme_sectors,
        external_markets=external_markets,
        historical_yields=HISTORICAL_YIELDS
    )

    # === 写入整页缓存（30s 内复用） ===
    # lite 首页首包不能写入完整缓存，否则后续基金/AI/市场页会拿到被瘦身的数据。
    if not lite:
        PORTFOLIO_CACHE["data"] = response
        PORTFOLIO_CACHE["saved_at"] = time.time()
        save_portfolio_to_db(response)

    return portfolio_response_for_client(response, lite)


async def background_portfolio_refresher():
    """Keep the full portfolio cache warm without making the foreground wait."""
    global BACKGROUND_PORTFOLIO_REFRESHING
    await asyncio.sleep(8)
    while True:
        interval = 60
        try:
            if not BACKGROUND_PORTFOLIO_REFRESHING:
                BACKGROUND_PORTFOLIO_REFRESHING = True
                started = time.time()
                BACKGROUND_PORTFOLIO_STATUS["last_started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                BACKGROUND_PORTFOLIO_STATUS["last_error"] = ""
                try:
                    await get_portfolio(force=1, lite=0)
                    BACKGROUND_PORTFOLIO_STATUS["last_finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    BACKGROUND_PORTFOLIO_STATUS["last_duration_seconds"] = round(time.time() - started, 2)
                    print(f"[后台刷新] 全量数据已更新 {datetime.now().strftime('%H:%M:%S')}")
                finally:
                    BACKGROUND_PORTFOLIO_REFRESHING = False
            disclosed = _is_today_disclosed()
            # Trading hours need a fresh DB snapshot for the 30s frontend header/status poll.
            # After close we only need to check NAV disclosure periodically.
            interval = 30 if is_trading_time() else (300 if not disclosed else 600)
            BACKGROUND_PORTFOLIO_STATUS["next_interval_seconds"] = interval
        except Exception as e:
            BACKGROUND_PORTFOLIO_REFRESHING = False
            interval = 60
            BACKGROUND_PORTFOLIO_STATUS["last_error"] = str(e)
            BACKGROUND_PORTFOLIO_STATUS["next_interval_seconds"] = interval
            print(f"[后台刷新] 异常: {e}")
        await asyncio.sleep(interval)


@app.post("/api/ai/deepseek", response_model=PortfolioResponse)
async def run_deepseek_analysis():
    """DeepSeek 分析暂时关闭，避免影响首屏速度和误触发。"""
    raise HTTPException(status_code=410, detail="DeepSeek 分析已暂时关闭")
    if not get_deepseek_api_key():
        raise HTTPException(status_code=400, detail="未配置 DEEPSEEK_API_KEY")

    if PORTFOLIO_CACHE["data"] is None:
        await get_portfolio(force=1)
    response = PORTFOLIO_CACHE["data"]
    response.funds = await enhance_funds_with_deepseek(
        response.funds,
        response.index,
        response.bond_index,
        response.news,
        strict=True,
    )
    PORTFOLIO_CACHE["data"] = response
    PORTFOLIO_CACHE["saved_at"] = time.time()
    return response


@app.get("/api/portfolio/status")
async def get_portfolio_status():
    """Lightweight status endpoint for the frontend header timestamp."""
    response, age = load_portfolio_snapshot_from_db()
    if response is None and PORTFOLIO_CACHE["data"] is not None:
        response = PORTFOLIO_CACHE["data"]
        age = time.time() - float(PORTFOLIO_CACHE.get("saved_at") or 0.0)
    if response is None:
        return {
            "ok": False,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "market_status": "unknown",
            "snapshot_age_seconds": None,
        }
    return {
        "ok": True,
        "date": response.date,
        "time": response.time,
        "market_status": response.market_status,
        "snapshot_age_seconds": round(float(age or 0.0), 1),
    }


@app.get("/api/refresh/status")
async def get_refresh_status():
    """Show backend refresh freshness for debugging foreground update speed."""
    now_ts = time.time()
    portfolio_age = None
    if PORTFOLIO_CACHE.get("saved_at"):
        portfolio_age = round(now_ts - float(PORTFOLIO_CACHE.get("saved_at") or 0.0), 1)
    db_response, db_age = load_portfolio_snapshot_from_db()
    return {
        "ok": True,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": market_status(),
        "is_trading_time": is_trading_time(),
        "portfolio_cache_age_seconds": portfolio_age,
        "db_snapshot_age_seconds": round(float(db_age), 1) if db_age is not None else None,
        "db_snapshot_time": f"{db_response.date} {db_response.time}" if db_response is not None else "",
        "news_age_seconds": round(now_ts - float(NEWS_CACHE.get("saved_at") or 0.0), 1) if NEWS_CACHE.get("saved_at") else None,
        "external_market_age_seconds": round(now_ts - float(EXTERNAL_MARKET_CACHE.get("saved_at") or 0.0), 1) if EXTERNAL_MARKET_CACHE.get("saved_at") else None,
        "background": BACKGROUND_PORTFOLIO_STATUS,
    }


@app.get("/api/news")
async def get_market_news(force: int = 0):
    """单独获取市场资讯。资讯页使用，不阻塞首页首屏。"""
    news = await fetch_market_news(force=bool(force))
    if PORTFOLIO_CACHE["data"] is not None:
        PORTFOLIO_CACHE["data"].news = news
        PORTFOLIO_CACHE["data"].theme_sectors = build_theme_sectors(
            PORTFOLIO_CACHE["data"].funds,
            PORTFOLIO_CACHE["data"].index,
            PORTFOLIO_CACHE["data"].bond_index,
            PORTFOLIO_CACHE["data"].hs300_index,
            PORTFOLIO_CACHE["data"].sz_index,
            news,
            k50_index=PORTFOLIO_CACHE["data"].k50_index,
        )
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "news": news,
        "theme_sectors": PORTFOLIO_CACHE["data"].theme_sectors if PORTFOLIO_CACHE["data"] is not None else [],
    }


@app.get("/api/cost-navs")
async def get_all_cost_navs():
    """获取所有基金的初始买入净值配置"""
    return {"cost_navs": COST_NAVS}


@app.post("/api/funds/watch")
async def add_watched_fund(payload: dict):
    """新增关注基金，并保存关注日期、买点阈值和历史收益率配置"""
    fund_code = str(payload.get("code", "")).strip()
    if not fund_code or len(fund_code) != 6 or not fund_code.isdigit():
        raise HTTPException(status_code=400, detail="基金代码必须是6位数字")

    follow_date = str(payload.get("follow_date") or datetime.now().strftime("%Y-%m-%d"))[:10]
    drop_threshold = float(payload.get("drop_threshold", 5.0) or 5.0)
    historical_yield = float(payload.get("historical_yield", 0.0) or 0.0)

    fund = await fetch_fund_from_eastmoney(fund_code, force=1)
    if not fund:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 未找到")

    FUND_SETTINGS[fund_code] = {
        "code": fund_code,
        "name": fund.name,
        "follow_date": follow_date,
        "historical_yield": round(historical_yield, 2),
        "historical_date": follow_date,
        "drop_threshold": round(drop_threshold, 2)
    }
    if not save_fund_settings(FUND_SETTINGS):
        raise HTTPException(status_code=500, detail="保存基金配置失败")

    if fund_code not in WATCHED_FUNDS:
        WATCHED_FUNDS.append(fund_code)
    HISTORICAL_YIELDS[fund_code] = {"yield": round(historical_yield, 2), "date": follow_date}
    BUY_POINT_CONFIG[fund_code] = {"drop_threshold": round(drop_threshold, 2)}
    ref_nav = round(float(fund.current_nav or 0.0), 4)
    ref_date = str(fund.nav_date or follow_date)[:10] or follow_date
    if ref_nav > 0:
        BUY_POINT_REFS[fund_code] = {"ref_nav": ref_nav, "ref_date": ref_date}
        if not save_buy_point_refs(BUY_POINT_REFS):
            raise HTTPException(status_code=500, detail="保存买点参考失败")

        # 新增关注基金不是实际买入，但也要落一条非持仓基准记录。
        # 这样买点监控、历史累计收益、删除清理都能围绕同一个 code 完整工作。
        COST_NAVS[fund_code] = {
            "buy_nav": ref_nav,
            "buy_date": follow_date,
            "buy_price": ref_nav,
            "shares": 0.0,
            "realized_yield_pct": 0.0,
            "yield_pct": round(historical_yield, 2),
            "total_return": round(historical_yield, 2),
            "transactions": [],
            "is_holding": False,
            "sell_date": "",
            "sell_price": 0.0
        }
        if not save_cost_navs_to_file(COST_NAVS):
            raise HTTPException(status_code=500, detail="保存历史收益失败")
        save_cost_navs_to_db(COST_NAVS)
    ensure_added_fund_tracking_baseline(fund_code, ref_nav, ref_date)
    FUND_DETAIL_CACHE.pop(fund_code, None)

    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()
    return {
        "ok": True,
        "code": fund_code,
        "name": fund.name,
        "follow_date": follow_date,
        "drop_threshold": round(drop_threshold, 2),
        "historical_yield": round(historical_yield, 2)
    }


@app.patch("/api/funds/watch/{fund_code}")
async def update_watched_fund(fund_code: str, payload: dict):
    """修改基金买点配置：下跌阈值和买点判断开始日期。"""
    fund_code = str(fund_code or "").strip()
    if not fund_code or len(fund_code) != 6 or not fund_code.isdigit():
        raise HTTPException(status_code=400, detail="基金代码必须是6位数字")
    if fund_code not in WATCHED_FUNDS:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 不在关注列表")

    try:
        drop_threshold = round(float(payload.get("drop_threshold", 5.0) or 5.0), 2)
    except Exception:
        raise HTTPException(status_code=400, detail="买点下跌阈值必须是数字")
    if drop_threshold <= 0 or drop_threshold > 80:
        raise HTTPException(status_code=400, detail="买点下跌阈值需在 0-80 之间")

    start_date = str(
        payload.get("ref_date")
        or payload.get("start_date")
        or payload.get("follow_date")
        or datetime.now().strftime("%Y-%m-%d")
    )[:10]
    if len(start_date) != 10:
        raise HTTPException(status_code=400, detail="买点判断开始日期格式不正确")

    fund = await fetch_fund_from_eastmoney(fund_code, force=1, lite=1)
    if not fund:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 未找到")

    history = await fetch_fund_history(fund_code, days=180, force=1)
    history_sorted = sorted(history or [], key=lambda x: x.date)
    ref_item = None
    for item in history_sorted:
        if item.date <= start_date:
            ref_item = item
        else:
            break
    if ref_item is None and history_sorted:
        ref_item = history_sorted[0]

    ref_nav = round(float(ref_item.nav if ref_item else fund.current_nav or 0.0), 4)
    ref_date = str(ref_item.date if ref_item else (fund.nav_date or start_date))[:10]
    if ref_nav <= 0:
        raise HTTPException(status_code=502, detail="未获取到有效参考净值")

    cfg = FUND_SETTINGS.get(fund_code, {}) if isinstance(FUND_SETTINGS.get(fund_code), dict) else {}
    cfg.update({
        "code": fund_code,
        "name": getattr(fund, "name", cfg.get("name", "")),
        "follow_date": start_date,
        "historical_date": start_date,
        "drop_threshold": drop_threshold,
        "historical_yield": round(float(cfg.get("historical_yield", HISTORICAL_YIELDS.get(fund_code, {}).get("yield", 0.0) if isinstance(HISTORICAL_YIELDS.get(fund_code), dict) else 0.0) or 0.0), 2)
    })
    FUND_SETTINGS[fund_code] = cfg
    if not save_fund_settings(FUND_SETTINGS):
        raise HTTPException(status_code=500, detail="保存基金配置失败")

    BUY_POINT_CONFIG[fund_code] = {"drop_threshold": drop_threshold}
    BUY_POINT_REFS[fund_code] = {"ref_nav": ref_nav, "ref_date": ref_date}
    if isinstance(HISTORICAL_YIELDS.get(fund_code), dict):
        HISTORICAL_YIELDS[fund_code]["date"] = start_date
    if not save_buy_point_refs(BUY_POINT_REFS):
        raise HTTPException(status_code=500, detail="保存买点参考失败")

    cost_data = COST_NAVS.get(fund_code, {}) if isinstance(COST_NAVS.get(fund_code), dict) else {}
    if cost_data and not cost_data.get("is_holding", False):
        cost_data.update({
            "buy_nav": ref_nav,
            "buy_date": start_date,
            "buy_price": ref_nav,
            "yield_pct": float(cfg.get("historical_yield", 0.0) or 0.0),
            "total_return": float(cfg.get("historical_yield", 0.0) or 0.0),
        })
        COST_NAVS[fund_code] = cost_data
        save_cost_navs_to_file(COST_NAVS)
        save_cost_navs_to_db(COST_NAVS)

    FUND_DETAIL_CACHE.pop(fund_code, None)
    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()
    return {
        "ok": True,
        "code": fund_code,
        "drop_threshold": drop_threshold,
        "ref_date": ref_date,
        "ref_nav": ref_nav,
    }


@app.delete("/api/funds/watch/{fund_code}")
async def delete_watched_fund(fund_code: str):
    """删除用户新增的关注基金；三只内置基金不允许删除。"""
    fund_code = str(fund_code or "").strip()
    if not fund_code or len(fund_code) != 6 or not fund_code.isdigit():
        raise HTTPException(status_code=400, detail="基金代码必须是6位数字")
    if fund_code in CORE_FUNDS:
        raise HTTPException(status_code=400, detail="内置基金不支持删除")
    if fund_code not in WATCHED_FUNDS and fund_code not in FUND_SETTINGS:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 不在关注列表")

    if fund_code in WATCHED_FUNDS:
        WATCHED_FUNDS.remove(fund_code)
    FUND_SETTINGS.pop(fund_code, None)
    HISTORICAL_YIELDS.pop(fund_code, None)
    BUY_POINT_CONFIG.pop(fund_code, None)
    BUY_POINT_REFS.pop(fund_code, None)
    COST_NAVS.pop(fund_code, None)
    NAV_HISTORY.pop(fund_code, None)
    FUND_DETAIL_CACHE.pop(fund_code, None)

    save_fund_settings(FUND_SETTINGS)
    save_buy_point_refs(BUY_POINT_REFS)
    save_cost_navs_to_file(COST_NAVS)
    save_cost_navs_to_db(COST_NAVS)
    save_nav_history(NAV_HISTORY)

    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()
    return {"ok": True, "code": fund_code, "deleted": True}


@app.post("/api/buy/{fund_code}")
async def confirm_buy(fund_code: str, payload: Optional[dict] = None):
    """
    用户买入/补仓指定基金。
    - buy_price: 买入净值，默认最新净值
    - shares: 份额权重，默认 1，用于加权平均成本
    - buy_date/date: 交易日期，默认最新净值日期
    """
    if fund_code not in WATCHED_FUNDS:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 不在关注列表")

    fund_latest = await fetch_fund_from_eastmoney(fund_code)
    if not fund_latest:
        raise HTTPException(status_code=502, detail=f"获取基金 {fund_code} 实时数据失败，请稍后重试")

    payload = payload if isinstance(payload, dict) else {}
    try:
        buy_nav = float(payload.get("buy_price") or payload.get("nav") or fund_latest.current_nav)
        shares = float(payload.get("shares") or 1.0)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="buy_price 和 shares 必须是有效数字")
    if buy_nav <= 0 or shares <= 0:
        raise HTTPException(status_code=400, detail="buy_price 和 shares 必须大于0")

    buy_date = str(payload.get("buy_date") or payload.get("date") or fund_latest.nav_date)
    old = COST_NAVS.get(fund_code, {}) if isinstance(COST_NAVS.get(fund_code), dict) else {}
    old_holding = bool(old.get("is_holding", False) and old.get("buy_nav", 0) > 0)
    old_shares = float(old.get("shares", 1.0 if old_holding else 0.0) or 0.0)
    old_cost = float(old.get("buy_nav", 0.0) or 0.0)
    new_shares = old_shares + shares
    avg_cost = round(((old_cost * old_shares) + (buy_nav * shares)) / new_shares, 4) if new_shares > 0 else round(buy_nav, 4)
    transactions = list(old.get("transactions", []))
    transactions.append({"type": "buy", "date": buy_date, "nav": round(buy_nav, 4), "shares": round(shares, 4)})

    COST_NAVS[fund_code] = {
        "buy_nav": avg_cost,
        "buy_date": old.get("buy_date") if old_holding else buy_date,
        "buy_price": avg_cost,
        "shares": round(new_shares, 4),
        "is_holding": True,
        "realized_yield_pct": float(old.get("realized_yield_pct", 0.0) or 0.0),
        "transactions": transactions
    }
    save_cost_navs_to_file(COST_NAVS)
    save_cost_navs_to_db(COST_NAVS)
    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()

    return {
        "code": fund_code,
        "name": fund_latest.name,
        "buy_price": round(buy_nav, 4),
        "cost_nav": avg_cost,
        "shares": round(new_shares, 4),
        "buy_date": COST_NAVS[fund_code]["buy_date"],
        "is_holding": True,
        "is_additional_buy": old_holding
    }


@app.post("/api/sell/{fund_code}")
async def confirm_sell(fund_code: str, payload: Optional[dict] = None):
    """
    用户卖出指定基金。支持部分卖出；全部卖出后恢复买点监控并更新买点参考日期。
    - sell_price: 卖出净值，默认最新净值
    - shares: 卖出份额权重，默认全部
    - sell_date/date: 卖出日期，默认最新净值日期
    """
    if fund_code not in WATCHED_FUNDS:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 不在关注列表")

    fund_info = await fetch_fund_from_eastmoney(fund_code)
    current_nav = fund_info.current_nav if fund_info else 0.0
    nav_date = fund_info.nav_date if fund_info else datetime.now().strftime("%Y-%m-%d")
    payload = payload if isinstance(payload, dict) else {}
    old = COST_NAVS.get(fund_code, {}) if isinstance(COST_NAVS.get(fund_code), dict) else {}
    if not old.get("is_holding", False) or float(old.get("buy_nav", 0) or 0) <= 0:
        raise HTTPException(status_code=400, detail=f"基金 {fund_code} 当前没有持仓记录")

    try:
        sell_nav = float(payload.get("sell_price") or payload.get("nav") or current_nav)
        old_shares = float(old.get("shares", 1.0) or 1.0)
        sell_shares = float(payload.get("shares") or old_shares)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="sell_price 和 shares 必须是有效数字")
    if sell_nav <= 0 or sell_shares <= 0:
        raise HTTPException(status_code=400, detail="sell_price 和 shares 必须大于0")
    if sell_shares > old_shares:
        raise HTTPException(status_code=400, detail="卖出份额不能大于当前份额")

    sell_date = str(payload.get("sell_date") or payload.get("date") or nav_date)
    cost_nav = float(old.get("buy_nav", 0.0) or 0.0)
    final_yield_pct = round((sell_nav - cost_nav) / cost_nav * 100, 2) if cost_nav > 0 else 0.0
    transactions = list(old.get("transactions", []))
    transactions.append({"type": "sell", "date": sell_date, "nav": round(sell_nav, 4), "shares": round(sell_shares, 4), "yield_pct": final_yield_pct})
    remain_shares = round(old_shares - sell_shares, 4)

    if remain_shares > 0:
        COST_NAVS[fund_code] = {
            "buy_nav": cost_nav,
            "buy_date": old.get("buy_date", ""),
            "buy_price": cost_nav,
            "shares": remain_shares,
            "is_holding": True,
            "realized_yield_pct": final_yield_pct,
            "transactions": transactions
        }
        is_holding = True
    else:
        COST_NAVS[fund_code] = {
            "buy_nav": cost_nav,
            "buy_date": old.get("buy_date", ""),
            "buy_price": cost_nav,
            "shares": 0.0,
            "is_holding": False,
            "sell_date": sell_date,
            "sell_price": round(sell_nav, 4),
            "realized_yield_pct": final_yield_pct,
            "transactions": transactions
        }
        is_holding = False
        BUY_POINT_REFS[fund_code] = {"ref_nav": round(sell_nav or current_nav, 4), "ref_date": sell_date}
        save_buy_point_refs(BUY_POINT_REFS)

    save_cost_navs_to_file(COST_NAVS)
    save_cost_navs_to_db(COST_NAVS)
    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()

    return {
        "code": fund_code,
        "is_holding": is_holding,
        "sell_price": round(sell_nav, 4),
        "sell_date": sell_date,
        "remaining_shares": remain_shares,
        "final_yield_pct": final_yield_pct,
        "buy_point_ref_date": sell_date if not is_holding else BUY_POINT_REFS.get(fund_code, {}).get("ref_date", ""),
        "buy_point_ref_nav": round((sell_nav if not is_holding else current_nav), 4)
    }


@app.post("/api/position/{fund_code}/cost")
async def correct_position_cost(fund_code: str, payload: Optional[dict] = None):
    """
    修正持仓成交价/成本价。
    只修正当前持仓成本，不新增买入流水；会追加一条 correction 记录方便回看。
    """
    if fund_code not in WATCHED_FUNDS:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 不在关注列表")

    payload = payload if isinstance(payload, dict) else {}
    old = COST_NAVS.get(fund_code, {}) if isinstance(COST_NAVS.get(fund_code), dict) else {}
    if not old.get("is_holding", False):
        raise HTTPException(status_code=400, detail=f"基金 {fund_code} 当前不是持仓中，不能修正成交价")

    try:
        cost_nav = float(payload.get("cost_nav") or payload.get("buy_price") or payload.get("nav"))
        shares = payload.get("shares")
        shares = float(shares) if shares not in (None, "") else float(old.get("shares", 1.0) or 1.0)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="cost_nav 和 shares 必须是有效数字")
    if cost_nav <= 0 or shares <= 0:
        raise HTTPException(status_code=400, detail="cost_nav 和 shares 必须大于0")

    correct_date = str(payload.get("date") or payload.get("buy_date") or old.get("buy_date") or datetime.now().strftime("%Y-%m-%d"))[:10]
    old_cost = float(old.get("buy_nav", 0.0) or 0.0)
    transactions = list(old.get("transactions", []))
    transactions.append({
        "type": "correct_cost",
        "date": correct_date,
        "old_nav": round(old_cost, 4),
        "nav": round(cost_nav, 4),
        "shares": round(shares, 4)
    })

    updated = dict(old)
    updated.update({
        "buy_nav": round(cost_nav, 4),
        "buy_price": round(cost_nav, 4),
        "buy_date": correct_date,
        "shares": round(shares, 4),
        "is_holding": True,
        "transactions": transactions
    })
    COST_NAVS[fund_code] = updated
    save_cost_navs_to_file(COST_NAVS)
    save_cost_navs_to_db(COST_NAVS)
    PORTFOLIO_CACHE["data"] = None
    PORTFOLIO_CACHE["saved_at"] = 0.0
    clear_portfolio_db_cache()

    return {
        "ok": True,
        "code": fund_code,
        "cost_nav": round(cost_nav, 4),
        "old_cost_nav": round(old_cost, 4),
        "shares": round(shares, 4),
        "buy_date": correct_date,
        "is_holding": True
    }


@app.get("/api/funds/{fund_code}", response_model=FundInfo)
async def get_fund(fund_code: str):
    """获取单个基金信息（含7天历史与买点）"""
    fund = await fetch_fund_from_eastmoney(fund_code)
    if not fund:
        raise HTTPException(status_code=404, detail=f"基金 {fund_code} 未找到")
    return fund


@app.get("/api/index/{index_code}", response_model=IndexInfo)
async def get_index(index_code: str):
    """获取指数信息"""
    if index_code == "000001" or index_code == "sh" or index_code == "sh000001":
        return await fetch_sh_index()
    raise HTTPException(status_code=404, detail=f"指数 {index_code} 未找到")


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "time": datetime.now().isoformat(), "version": "2.1.0"}


@app.get("/api/index-model-stats")
async def get_index_model_stats_api():
    """获取基金专用模型（指数跟随 / 多因子）残差统计"""
    result = {}
    for code, model in FUND_SPECIFIC_MODELS.items():
        stats = INDEX_RESIDUAL_CACHE.get(code, {})
        result[code] = {
            "model_type": model.get("type"),
            "model_description": model.get("description", ""),
            "benchmark": model.get("benchmark_name") or "+".join([f.get("name","") for f in model.get("factors",[])]),
            "samples": stats.get("samples", []),
            "mean_residual": stats.get("mean_residual", 0.0),
            "std_residual": stats.get("std_residual", 0.0),
            "sample_count": stats.get("sample_count", 0),
            "confidence": stats.get("confidence", "none"),
            "last_residual": stats.get("last_residual", 0.0),
            "last_update": stats.get("last_update", ""),
            "enabled": get_index_model_estimate(code)["enabled"]
        }
    return result


@app.get("/api/correction-stats")
async def get_correction_stats_api():
    """获取残差修正模型统计（调试用）
    返回每只基金的残差样本、均值、标准差、置信度
    """
    result = {}
    for code in ["011609", "020741", "004746"]:
        stats = get_correction_stats(code)
        eff = get_effective_correction(code)
        result[code] = {
            "samples": stats.get("samples", []),
            "mean_residual": stats.get("mean_residual", 0.0),
            "std_residual": stats.get("std_residual", 0.0),
            "sample_count": stats.get("sample_count", 0),
            "confidence": stats.get("confidence", "none"),
            "last_residual": stats.get("last_residual", 0.0),
            "last_update": stats.get("last_update", ""),
            "effective_offset": eff["offset"],
            "enabled": eff["enabled"]
        }
    result["_meta"] = CORRECTION_CACHE.get("_meta", {})
    return result


@app.get("/api/model-accuracy")
async def get_model_accuracy_api(days: int = 7):
    """模型准确性评估：基于 correction_cache 中积累的 est vs actual 样本
    返回每只基金的 MAE、方向胜率、按日期分组的残差明细
    """
    from collections import defaultdict
    result = {}
    for code in ["011609", "020741", "004746"]:
        stats = get_correction_stats(code)
        samples = stats.get("samples", [])
        # 按日期去重（每日多条 time 快照只保留最后一条 = 最接近收盘的预测）
        by_date = {}
        for s in samples:
            d = s.get("date", "")
            if not d:
                continue
            # 跳过 manual_test
            t = s.get("time", "")
            if t in ("manual_test", ""):
                # 无 time 的视为收盘后回填，优先级最高
                by_date[d] = s
            else:
                # 有 time 的取最后一个（时间最晚的 = 最接近 14:32 收盘预测）
                if d not in by_date or by_date[d].get("time", "") < t:
                    by_date[d] = s
        # 限制最近 N 天
        sorted_dates = sorted(by_date.keys(), reverse=True)[:days]
        deduped = [by_date[d] for d in sorted_dates]
        n = len(deduped)
        if n == 0:
            result[code] = {
                "sample_count": 0, "mae": 0, "rmse": 0,
                "direction_accuracy": 0, "mean_residual": 0,
                "last_residual": 0, "last_actual": 0, "last_est": 0,
                "last_date": "", "details": []
            }
            continue
        abs_errs = [abs(s["residual"]) for s in deduped]
        sq_errs = [s["residual"] ** 2 for s in deduped]
        # 方向胜率：实际涨跌方向与预测方向是否一致
        dir_correct = 0
        for s in deduped:
            pred = s["est_change"]
            actual = s["actual_change"]
            if abs(pred) < 0.005 and abs(actual) < 0.005:
                continue  # 双方都接近 0，跳过（债基常态）
            if (pred >= 0 and actual >= 0) or (pred < 0 and actual < 0):
                dir_correct += 1
        mae = sum(abs_errs) / n
        rmse = (sum(sq_errs) / n) ** 0.5
        mean_res = sum(s["residual"] for s in deduped) / n
        last = deduped[0]
        result[code] = {
            "sample_count": n,
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
            "direction_accuracy": round(dir_correct / n * 100, 1) if n > 0 else 0,
            "mean_residual": round(mean_res, 4),
            "last_residual": last.get("residual", 0),
            "last_actual": last.get("actual_change", 0),
            "last_est": last.get("est_change", 0),
            "last_date": last.get("date", ""),
            "details": [{"date": s.get("date"), "est": s.get("est_change"),
                          "actual": s.get("actual_change"),
                          "residual": s.get("residual")} for s in deduped]
        }
    return result


@app.post("/api/correction-stats/reset")
async def reset_correction_stats():
    """重置残差缓存（调试用）"""
    global CORRECTION_CACHE
    CORRECTION_CACHE = {"_meta": {"last_update": "", "max_samples": 20}}
    save_correction_cache(CORRECTION_CACHE)
    return {"status": "ok", "message": "残差缓存已重置"}


@app.post("/api/correction-stats/inject")
async def inject_correction_sample(payload: dict):
    """手动注入一条残差样本（用于冷启动或回填）
    payload: {code, date, est_change, actual_change}
    """
    code = payload.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少基金代码 code")
    try:
        est = float(payload.get("est_change", 0))
        actual = float(payload.get("actual_change", 0))
        date = payload.get("date", datetime.now().strftime("%Y-%m-%d"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="est_change/actual_change 必须为数字")
    if est == 0 or actual == 0:
        raise HTTPException(status_code=400, detail="est_change/actual_change 不能为 0")
    record_residual(code, est, actual, date)
    eff = get_effective_correction(code)
    return {
        "status": "ok",
        "code": code,
        "sample_added": {"date": date, "est_change": est, "actual_change": actual, "residual": round(actual - est, 3)},
        "stats": get_correction_stats(code),
        "effective": eff
    }


@app.post("/api/snapshots/take")
async def take_snapshot_now(payload: Optional[dict] = None):
    """手动触发盘中快照采样（测试 / 立即采集）
    payload: {codes: ["011609", ...], time: "HH:MM"} (可选)
    """
    codes = (payload or {}).get("codes", SNAPSHOT_FUND_CODES)
    snap_time = (payload or {}).get("time", datetime.now().strftime("%H:%M"))
    results = []
    for c in codes:
        if c not in SNAPSHOT_FUND_CODES:
            results.append({"code": c, "status": "skipped", "reason": "not in watchlist"})
            continue
        await take_intraday_snapshot(c, snap_time)
        results.append({"code": c, "status": "ok", "time": snap_time})
    return {"status": "ok", "results": results, "snapshots": INTRADAY_SNAPSHOTS}


@app.get("/api/snapshots")
async def get_snapshots():
    """查看所有盘中快照"""
    return INTRADAY_SNAPSHOTS


@app.post("/api/snapshots/reset")
async def reset_snapshots():
    """重置盘中快照缓存"""
    global INTRADAY_SNAPSHOTS
    INTRADAY_SNAPSHOTS = {"_meta": {"max_snapshots_per_fund": 60, "snapshot_times": SNAPSHOT_TIMES}}
    save_intraday_snapshots(INTRADAY_SNAPSHOTS)
    return {"status": "ok", "message": "盘中快照已重置"}


@app.get("/api/trading-calendar")
async def get_trading_calendar(start: str = "", end: str = ""):
    """查看 A 股交易日历
    start/end: YYYY-MM-DD（可选），列出此区间内的所有日期及是否为交易日
    """
    cal = load_trading_calendar()
    holidays = set(cal.get("holidays", []))
    result = {"holidays_count": len(holidays), "is_today_trading_day": is_trading_day()}
    if start and end:
        try:
            from datetime import date as _date, timedelta as _td
            d0 = _date.fromisoformat(start)
            d1 = _date.fromisoformat(end)
            days = []
            cur = d0
            while cur <= d1:
                ds = cur.strftime("%Y-%m-%d")
                is_td = (cur.weekday() < 5) and (ds not in holidays)
                days.append({"date": ds, "weekday": cur.weekday(), "is_trading_day": is_td})
                cur += _td(days=1)
            result["days"] = days
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {e}")
    else:
        result["holidays"] = sorted(holidays)
    return result


@app.post("/api/trading-calendar/holiday")
async def add_holiday(payload: dict):
    """添加/删除休市日
    payload: {date: "YYYY-MM-DD", action: "add"|"remove"}
    """
    date_str = payload.get("date", "").strip()
    action = payload.get("action", "add").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="缺少 date")
    cal = load_trading_calendar()
    holidays = set(cal.get("holidays", []))
    if action == "add":
        holidays.add(date_str)
    elif action == "remove":
        holidays.discard(date_str)
    else:
        raise HTTPException(status_code=400, detail="action 必须是 add 或 remove")
    cal["holidays"] = sorted(holidays)
    save_trading_calendar(cal)
    return {"status": "ok", "date": date_str, "action": action, "total_holidays": len(holidays)}


# ============= 新闻正文提取（不离开 APP 阅读） =============

# 简单内存缓存：URL -> (timestamp, content_dict)，避免重复抓取
_NEWS_CONTENT_CACHE: Dict[str, tuple] = {}
_NEWS_CACHE_TTL = 600  # 10 分钟


@app.get("/api/news-content")
async def get_news_content(url: str):
    """抓取并提取新闻正文（纯文本，不含图片），10 分钟缓存"""
    import time as _t
    import html as _html
    now = _t.time()

    # 缓存命中
    if url in _NEWS_CONTENT_CACHE:
        ts, data = _NEWS_CONTENT_CACHE[url]
        if now - ts < _NEWS_CACHE_TTL:
            return data

    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL 不合法")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=HTTP_HEADERS)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return {"url": url, "ok": False, "error": f"抓取失败: {e}"}

    # 优先用 trafilatura
    title = ""
    content = ""
    date_str = ""
    try:
        import trafilatura
        extracted_json = trafilatura.extract(
            html,
            include_images=False,
            include_links=False,
            include_comments=False,
            include_tables=False,
            output='json',
            with_metadata=True,
        )
        if extracted_json:
            import json as _json
            meta = _json.loads(extracted_json)
            title = meta.get("title", "") or ""
            content = meta.get("text", "") or ""
            date_str = meta.get("date", "") or ""
    except Exception:
        pass

    def _clean_lines(raw: str) -> List[str]:
        raw = _html.unescape(raw or "")
        raw = re.sub(r"\r", "\n", raw)
        raw = re.sub(r"[ \t\u3000]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        bad_patterns = [
            r"责任编辑[:：]?.*",
            r"风险提示[:：]?.*",
            r"免责声明[:：]?.*",
            r"本文来自.*",
            r"文章来源[:：]?.*",
            r"举报.*",
            r"分享.*",
            r"打开APP.*",
            r"下载.*APP.*",
            r"点击.*查看.*",
            r"海量资讯.*",
            r"广告.*",
        ]
        lines: List[str] = []
        seen = set()
        for line in raw.split("\n"):
            line = line.strip(" \t　\r\n")
            if len(line) < 9:
                continue
            if any(re.search(p, line, re.I) for p in bad_patterns):
                continue
            if re.fullmatch(r"[\d\s:：/\-.年月日]+", line):
                continue
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return lines

    content_lines = _clean_lines(content)
    if content_lines:
        content = "\n\n".join(content_lines[:120])

    # 回退：BeautifulSoup 多候选提取
    if not content:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            if not title and soup.title:
                title = soup.title.get_text(strip=True)
            meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
            if meta_title and meta_title.get("content"):
                title = title or meta_title.get("content", "").strip()
            meta_date = (
                soup.find("meta", attrs={"property": "article:published_time"})
                or soup.find("meta", attrs={"name": "publishdate"})
                or soup.find("meta", attrs={"name": "pubdate"})
                or soup.find("meta", attrs={"name": "date"})
            )
            if meta_date and meta_date.get("content"):
                date_str = date_str or meta_date.get("content", "").strip()
            for s in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]):
                s.decompose()
            selectors = [
                "article",
                "main",
                '[class*="article"]',
                '[id*="article"]',
                '[class*="content"]',
                '[id*="content"]',
                '[class*="detail"]',
                '[id*="detail"]',
                '[class*="text"]',
                '[class*="body"]',
                '[id*="body"]',
            ]
            candidates = []
            for selector in selectors:
                candidates.extend(soup.select(selector)[:8])
            if soup.body:
                candidates.append(soup.body)

            best_lines: List[str] = []
            best_score = 0
            for node in candidates:
                text = node.get_text(separator="\n", strip=True)
                lines = _clean_lines(text)
                long_count = sum(1 for ln in lines if len(ln) >= 28)
                score = sum(len(ln) for ln in lines) + long_count * 80
                if score > best_score:
                    best_score = score
                    best_lines = lines

            content = "\n\n".join(best_lines[:160])
        except Exception as e:
            return {"url": url, "ok": False, "error": f"解析失败: {e}"}

    # 截断过长内容
    if len(content) > 8000:
        content = content[:8000] + "\n\n...(已截断)"
    if not content or len(content.strip()) < 40:
        return {"url": url, "ok": False, "error": "未提取到有效正文", "title": title, "date": date_str}

    data = {
        "url": url,
        "ok": True,
        "title": title,
        "content": content,
        "date": date_str,
        "length": len(content),
    }
    _NEWS_CONTENT_CACHE[url] = (now, data)
    return data


@app.get("/cert")
async def download_cert():
    """下载SSL证书，用于iOS/Mac设备安装信任"""
    cert_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert.pem")
    return FileResponse(
        cert_path,
        media_type="application/x-x509-ca-cert",
        filename="frp-oil.com.pem",
        headers={"Content-Disposition": "attachment; filename=frp-oil.com.pem"}
    )


@app.get("/")
async def root():
    """返回前端HTML页面"""
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.get("/manifest.json")
async def manifest():
    """返回 PWA manifest.json"""
    manifest_path = os.path.join(web_app_dir, "manifest.json")
    return FileResponse(
        manifest_path,
        media_type="application/manifest+json",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


# ============= SPA fallback: 所有未匹配的路由返回 index.html =============
# 解决 iOS 主屏幕（Standalone 模式）下可能因为 URL 路径差异导致 Not Found 的问题
# 使用 FastAPI 内置的 path 参数捕获所有未匹配路径

# ===== v3 双密码 + 新界面 =====
V3_PASSWORD = "v3pass"
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/api/auth")
async def auth(password: str = ""):
    if password == V3_PASSWORD:
        return {"ok": True, "version": "v3"}
    return {"ok": False, "version": ""}

@app.get("/v3/{full_path:path}")
async def serve_v3(full_path: str = ""):
    index_path = os.path.join(STATIC_DIR, "..", "index_v3.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    return JSONResponse({"error": "v3 page not found"}, status_code=404)

@app.get("/v4")
@app.get("/v4/{full_path:path}")
async def serve_v4(full_path: str = ""):
    index_path = os.path.join(STATIC_DIR, "..", "index_v4.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    return JSONResponse({"error": "v4 page not found"}, status_code=404)

@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """SPA fallback：未匹配到 API/静态文件的路由均返回 index.html"""
    # 跳过已知的 API 和静态文件前缀（理论上不会匹配到，但作为安全防护）
    if full_path.startswith("api/") or full_path.startswith("static/") or full_path.startswith("manifest.json"):
        return JSONResponse({"error": "Not Found"}, status_code=404)
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8001"))
    )

