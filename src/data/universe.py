"""
Universe Sync — Fetch full A-share stock list from Eastmoney.

Phase: Commit 1 — Stock Identity Infrastructure
"""

import os
import time
import glob
import requests
import pandas as pd
from datetime import date
from src.data.security_master import SecurityMaster
from src.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_eastmoney_stock_list() -> pd.DataFrame:
    """Fetch沪深京 full A-share list from Eastmoney.

    Falls back to a built-in sample universe (~200 stocks) if network unavailable.
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "6000", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f12,f13,f14,f100,f20,f21,f117,f116,f168",
        "_": str(int(time.time()))
    }

    try:
        s = requests.Session()
        s.trust_env = False  # Bypass proxy
        resp = s.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("universe: Eastmoney unavailable, using built-in sample universe: %s", e)
        return _sample_universe()

    records = []
    raw = data.get("data")
    if not raw or not raw.get("diff"):
        return _sample_universe()

    for item in raw["diff"]:
        code = str(item.get("f12", "")).strip()
        if not code.isdigit() or len(code) != 6:
            continue
        exchange = "SH" if code.startswith("6") else "SZ"
        if item.get("f13") == 2:
            exchange = "BJ"
        name = item.get("f14", "")
        records.append({
            "security_id": f"{code}.{exchange}",
            "code": code,
            "exchange": exchange,
            "name": name,
            "industry": item.get("f100", ""),
            "total_mv": round(item.get("f20", 0) / 1e8, 2) if item.get("f20") else 0,
            "float_mv": round(item.get("f21", 0) / 1e8, 2) if item.get("f21") else 0,
            "avg_turnover_20d": round(item.get("f168", 0), 2),
            "avg_amount_20d": round(item.get("f117", 0) / 1e4, 2) if item.get("f117") else 0,
            "is_st": 1 if "ST" in name else 0,
        })

    return pd.DataFrame(records)


def _sample_universe() -> pd.DataFrame:
    """Built-in sample universe — ~200 major A-share stocks."""
    stocks = [
        # 上证50成分
        ("600519", "SH", "贵州茅台"), ("601318", "SH", "中国平安"),
        ("600036", "SH", "招商银行"), ("600276", "SH", "恒瑞医药"),
        ("601888", "SH", "中国中免"), ("600030", "SH", "中信证券"),
        ("600887", "SH", "伊利股份"), ("601166", "SH", "兴业银行"),
        ("600900", "SH", "长江电力"), ("601398", "SH", "工商银行"),
        ("601288", "SH", "农业银行"), ("601939", "SH", "建设银行"),
        ("601628", "SH", "中国人寿"), ("600585", "SH", "海螺水泥"),
        ("600048", "SH", "保利发展"), ("600809", "SH", "山西汾酒"),
        ("600690", "SH", "海尔智家"), ("601012", "SH", "隆基绿能"),
        ("600031", "SH", "三一重工"), ("600309", "SH", "万华化学"),
        # 深证
        ("000858", "SZ", "五粮液"), ("000333", "SZ", "美的集团"),
        ("002415", "SZ", "海康威视"), ("000001", "SZ", "平安银行"),
        ("002594", "SZ", "比亚迪"), ("000651", "SZ", "格力电器"),
        ("002475", "SZ", "立讯精密"), ("000568", "SZ", "泸州老窖"),
        ("002714", "SZ", "牧原股份"), ("000725", "SZ", "京东方A"),
        ("002142", "SZ", "宁波银行"), ("000002", "SZ", "万科A"),
        ("002352", "SZ", "顺丰控股"), ("001979", "SZ", "招商蛇口"),
        ("300750", "SZ", "宁德时代"), ("300059", "SZ", "东方财富"),
        ("300760", "SZ", "迈瑞医疗"), ("300015", "SZ", "爱尔眼科"),
        ("300124", "SZ", "汇川技术"), ("300274", "SZ", "阳光电源"),
        ("300498", "SZ", "温氏股份"), ("300122", "SZ", "智飞生物"),
        # 沪深300扩展
        ("600000", "SH", "浦发银行"), ("600016", "SH", "民生银行"),
        ("600050", "SH", "中国联通"), ("600104", "SH", "上汽集团"),
        ("600196", "SH", "复星医药"), ("600406", "SH", "国电南瑞"),
        ("600436", "SH", "片仔癀"), ("600438", "SH", "通威股份"),
        ("600570", "SH", "恒生电子"), ("600588", "SH", "用友网络"),
        ("600660", "SH", "福耀玻璃"), ("600741", "SH", "华域汽车"),
        ("600745", "SH", "闻泰科技"), ("600795", "SH", "国电电力"),
        ("601088", "SH", "中国神华"), ("601111", "SH", "中国国航"),
        ("601138", "SH", "工业富联"), ("601211", "SH", "国泰君安"),
        ("601390", "SH", "中国中铁"), ("601600", "SH", "中国铝业"),
        ("601601", "SH", "中国太保"), ("601668", "SH", "中国建筑"),
        ("601688", "SH", "华泰证券"), ("601728", "SH", "中国电信"),
        ("601766", "SH", "中国中车"), ("601818", "SH", "光大银行"),
        ("601857", "SH", "中国石油"), ("601919", "SH", "中远海控"),
        ("601985", "SH", "中国核电"), ("603259", "SH", "药明康德"),
        ("603288", "SH", "海天味业"), ("603501", "SH", "韦尔股份"),
        ("000063", "SZ", "中兴通讯"), ("000100", "SZ", "TCL科技"),
        ("000157", "SZ", "中联重科"), ("000338", "SZ", "潍柴动力"),
        ("000625", "SZ", "长安汽车"), ("000776", "SZ", "广发证券"),
        ("000895", "SZ", "双汇发展"), ("002049", "SZ", "紫光国微"),
        ("002129", "SZ", "TCL中环"), ("002230", "SZ", "科大讯飞"),
        ("002241", "SZ", "歌尔股份"), ("002271", "SZ", "东方雨虹"),
        ("002304", "SZ", "洋河股份"), ("002460", "SZ", "赣锋锂业"),
        ("002466", "SZ", "天齐锂业"), ("002555", "SZ", "三七互娱"),
        ("300014", "SZ", "亿纬锂能"), ("300033", "SZ", "同花顺"),
        ("300316", "SZ", "晶盛机电"), ("300347", "SZ", "泰格医药"),
        ("300408", "SZ", "三环集团"), ("300413", "SZ", "芒果超媒"),
        ("300450", "SZ", "先导智能"), ("300496", "SZ", "中科创达"),
        ("300529", "SZ", "健帆生物"), ("300661", "SZ", "圣邦股份"),
        ("300782", "SZ", "卓胜微"), ("300896", "SZ", "爱美客"),
    ]
    records = []
    for code, exchange, name in stocks:
        records.append({
            "security_id": f"{code}.{exchange}",
            "code": code, "exchange": exchange, "name": name,
            "industry": "", "total_mv": 0, "float_mv": 0,
            "avg_turnover_20d": 0.0, "avg_amount_20d": 0.0, "is_st": 0,
        })
    return pd.DataFrame(records)


def sync_security_master(db_path: str = "data/cache.db"):
    """Full sync of Eastmoney stock list → security_master table."""
    logger.info("📡 Fetching A-share stock list from Eastmoney...")
    df = fetch_eastmoney_stock_list()

    if df.empty:
        logger.warning("❌ No stocks fetched — check network or API status")
        return 0

    # Supplement missing fields
    df['ipo_date'] = None
    df['list_days'] = 0
    df['status'] = 'active'
    df['industry_index'] = None
    df['is_new_stock'] = 0

    master = SecurityMaster(db_path)
    master.upsert(df.to_dict(orient='records'))

    count = master.count()
    logger.info(f"✅ Synced {count} stocks to security_master")

    # Breakdown
    sh = df[df['exchange'] == 'SH'].shape[0]
    sz = df[df['exchange'] == 'SZ'].shape[0]
    bj = df[df['exchange'] == 'BJ'].shape[0]
    st = df[df['is_st'] == 1].shape[0]
    logger.info(f"   SH: {sh}, SZ: {sz}, BJ: {bj}, ST: {st}")

    return count


# ═══════════════════════════════════════════════════════════
# Offline universe sync from local TDX (通达信) vipdoc
# ═══════════════════════════════════════════════════════════
#
# Eastmoney is unreachable in some networks (proxy blocks its host), so we
# derive the tradable universe straight from the local .day files instead.
# This guarantees the universe always matches the K-line files we can actually
# read, and needs no eastmoney round-trip. Names / market cap / ST flags are
# enriched from Tencent (qt.gtimg.cn) which *is* reachable with proxy bypass.

# Real-stock code prefixes per market — excludes indices / ETF / LOF /
# 可转债 / B股 that also live under the same vipdoc/lday folders.
_STOCK_PREFIXES = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301"),
}


def _is_stock(market: str, code: str) -> bool:
    """True iff (market, code) is a tradable A-share (not index/ETF/bond)."""
    if market == "sh":
        return code[:3] in _STOCK_PREFIXES["sh"]
    if market == "sz":
        return code[:3] in _STOCK_PREFIXES["sz"]
    if market == "bj":
        # 北交所: 920x plus legacy 4x/8x listings; exclude 899x(index)/810x.
        if code[:3] in ("899", "810"):
            return False
        return code[:3] == "920" or code[0] in ("4", "8")
    return False


def scan_local_universe(vipdoc_root: str = None) -> list:
    """Scan TDX vipdoc .day files → list of {code, exchange} dicts.

    Fully offline. If vipdoc_root is None it is resolved via LocalDataProvider
    (env overrides + default drive paths).
    """
    from src.data.local_provider import LocalDataProvider

    if vipdoc_root is None:
        vipdoc_root = LocalDataProvider()._resolve_tdx_root()
    if not vipdoc_root:
        return []

    ex_map = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
    out = []
    for mk in ("sh", "sz", "bj"):
        lday = os.path.join(vipdoc_root, mk, "lday")
        if not os.path.isdir(lday):
            continue
        for fp in glob.glob(os.path.join(lday, f"{mk}*.day")):
            code = os.path.basename(fp)[2:8]
            if len(code) == 6 and code.isdigit() and _is_stock(mk, code):
                out.append({"code": code, "exchange": ex_map[mk]})
    return out


def tencent_batch_quotes(codes: list, batch_size: int = 60,
                         pause: float = 0.1) -> dict:
    """Batch real-time quotes from Tencent, proxy-bypassed.

    Returns {code: {name, price, pe_ttm, pb, mcap, float_mcap, turnover_pct}}.
    Tencent's qt.gtimg.cn is reachable without the system proxy, so we set
    trust_env=False (the main DataProvider historically did NOT, which is why
    every quote came back empty behind a corporate proxy).
    """
    s = requests.Session()
    s.trust_env = False
    result = {}

    def _num(fields, idx):
        try:
            return float(fields[idx]) if fields[idx] else None
        except (ValueError, IndexError):
            return None

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        prefixed = []
        for c in batch:
            c = str(c).zfill(6)
            if c.startswith(("00", "30")):
                prefixed.append(f"sz{c}")
            elif c.startswith(("60", "68")):
                prefixed.append(f"sh{c}")
            elif c.startswith(("4", "8", "9")):
                prefixed.append(f"bj{c}")
            else:
                prefixed.append(f"sh{c}")
        try:
            r = s.get(f"https://qt.gtimg.cn/q={','.join(prefixed)}", timeout=10)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "=" not in line:
                    continue
                var, raw = line.split("=", 1)
                f = raw.strip().strip('";').split("~")
                if len(f) < 10:
                    continue
                code = var.strip()[-6:]
                result[code] = {
                    "name": f[1] if len(f) > 1 else "",
                    "price": _num(f, 3),
                    "pe_ttm": _num(f, 39),
                    "pb": _num(f, 46),
                    "mcap": _num(f, 45),         # 总市值 (亿元)
                    "float_mcap": _num(f, 44),   # 流通市值 (亿元)
                    "turnover_pct": _num(f, 38),
                }
        except Exception as e:
            logger.warning("universe: Tencent batch @%d failed: %s", i, e)
        if pause:
            time.sleep(pause)
    return result


def sync_security_master_from_local(vipdoc_root: str = None,
                                    db_path: str = "data/cache.db",
                                    enrich: bool = True) -> int:
    """Offline-first universe sync from local TDX data.

    1. Scan vipdoc → tradable universe (code, exchange).
    2. Enrich name / market cap / ST flag via Tencent (proxy-bypassed).
    3. Rebuild security_master (existing names kept as fallback).

    Returns the resulting security_master row count.
    """
    rows = scan_local_universe(vipdoc_root)
    if not rows:
        logger.warning("❌ No local .day files found — check TDX vipdoc path "
              "(STOCK_SIEVE_TDX_VIPDOC / STOCK_SIEVE_TDX_ROOT).")
        return 0
    logger.info(f"📁 Scanned local universe: {len(rows)} stocks")

    master = SecurityMaster(db_path)

    existing = {}
    try:
        existing = {r[0]: r[1] for r in
                    master.db.execute("SELECT code, name FROM security_master").fetchall()}
    except Exception as e:
        logger.warning("universe: load existing security_master names failed: %s", e)

    quotes = {}
    if enrich:
        logger.info("📡 Enriching name/PE/PB/mcap via Tencent (proxy-bypassed)...")
        quotes = tencent_batch_quotes([r["code"] for r in rows])
        logger.info(f"   got {len(quotes)} quotes")

    records = []
    for r in rows:
        code = r["code"]
        q = quotes.get(code, {})
        name = q.get("name") or existing.get(code) or code
        is_st = 1 if "ST" in name.upper() else 0
        records.append({
            "security_id": f"{code}.{r['exchange']}",
            "code": code, "exchange": r["exchange"], "name": name,
            "ipo_date": None, "list_days": 0, "status": "active",
            "industry": "", "industry_index": None,
            "total_mv": q.get("mcap") or 0, "float_mv": q.get("float_mcap") or 0,
            "avg_turnover_20d": q.get("turnover_pct") or 0.0, "avg_amount_20d": 0.0,
            "is_st": is_st, "is_new_stock": 0,
        })

    # --- TRUE rebuild: purge stale rows not present in the fresh local scan ---
    # upsert() is INSERT OR REPLACE, so it never removes old garbage (板块指数
    # 880/881, ETF 159/5xx, 可转债 11x/12x) that a previous eastmoney sync left
    # behind. Delete everything outside the clean set before writing.
    fresh_ids = {rec["security_id"] for rec in records}
    stale = [sid for sid in
             (row[0] for row in
              master.db.execute("SELECT security_id FROM security_master").fetchall())
             if sid not in fresh_ids]
    if stale:
        master.db.executemany(
            "DELETE FROM security_master WHERE security_id=?",
            [(sid,) for sid in stale])
        master.db.commit()
        logger.info(f"🧹 Purged {len(stale)} stale rows (indices/ETF/bonds/delisted)")

    master.upsert(records)

    count = master.count()
    sh = sum(1 for r in rows if r["exchange"] == "SH")
    sz = sum(1 for r in rows if r["exchange"] == "SZ")
    bj = sum(1 for r in rows if r["exchange"] == "BJ")
    st = sum(1 for rec in records if rec["is_st"])
    logger.info(f"✅ security_master now {count} stocks  (SH {sh}, SZ {sz}, BJ {bj}, ST {st})")
    return count
