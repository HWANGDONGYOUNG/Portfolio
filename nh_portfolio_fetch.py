"""
나무증권(NH투자증권) Namuh PLUG API로 보유 종목 + 관심종목의 시세·투자자별 수급을 조회해
같은 폴더에 JSON 파일로 저장하는 스크립트.

- 조회만 합니다. 주문·계좌 API는 전혀 호출하지 않습니다.
- App Key / Secret 은 이 스크립트나 이 폴더에 넣지 마세요.
  C:\\Users\\<사용자>\\.nhplug\\.env 에 넣으면 SDK(nhplug)가 자동으로 읽습니다.

사용한 API (NH 공식 명세 기준)
- POST /krstock/quote/v1/currentPrice     주식현재가 시세
- POST /krstock/quote/v1/currentInvestor  주식현재가 투자자 (일자별 개인/외국인/기관 순매수)

실행:  python nh_portfolio_fetch.py
결과:  nh_data_latest.json (항상 최신본으로 덮어씀) + data\\nh_data_YYYYMMDD_HHMM.json (기록용)
       - "stocks": 보유 종목 (평균단가 대비 손익 포함)
       - "watchlist": 미보유 관심종목 (평균단가/손익 없음, 시세·수급만)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

try:
    from nhplug import call
    from nhplug.client import status_of
    from nhplug.errors import NhplugError
except ImportError:
    print("nhplug 패키지가 없습니다. 먼저 실행하세요:  pip install nhplug")
    sys.exit(1)

# ── 조회 대상 ────────────────────────────────────────────────
# 보유 종목이 바뀌면 여기만 고치세요. avg(평균매입단가)가 있는 것만 손익(%)을 계산합니다.
HOLDINGS = [
    {"name": "우리금융지주", "code": "316140", "qty": 300, "avg": 34300},
    {"name": "KT&G",        "code": "033780", "qty": 30,  "avg": 171266},
    {"name": "KB금융",      "code": "105560", "qty": 20,  "avg": 175406},
    {"name": "대한항공",    "code": "003490", "qty": 1,   "avg": 31800},
]
# 미보유 관심종목 (시세·수급만 확인, 손익 계산 없음)
WATCHLIST = [
    {"name": "삼성전자",   "code": "005930"},
    {"name": "SK하이닉스", "code": "000660"},
]
INVESTOR_DAYS = 10          # 투자자별 수급 조회 일수
# 시세: UNT=KRX+NXT 통합, KRX=정규장 기준. 둘 다 저장해 비교할 수 있게 함.
PRICE_MARKETS = ["UNT", "KRX"]
INVESTOR_MARKET = "KRX"     # 거래소 투자자별 집계 기준

OUT_DIR = Path(__file__).resolve().parent
HIST_DIR = OUT_DIR / "data"

PRICE_FIELDS = [
    "iem_nm", "stck_prpr", "prdy_vrss_sign", "prdy_vrss", "prdy_ctrt",
    "stck_oprc", "stck_hgpr", "stck_lwpr", "acml_vol", "acml_tr_pbmn", "hoga_bsop_hour",
]


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _block(data, key="Output_0"):
    """응답 블록은 데이터가 있을 때만 내려온다(명세). 없으면 None."""
    return data.get(key) if isinstance(data, dict) else None


def fetch_price(code: str, market: str) -> dict:
    data = call("/krstock/quote/v1/currentPrice", {"iem_cd": code, "market_cd": market})
    cd, msg = status_of(data)
    out = _block(data) or {}
    return {
        "market": market,
        "rsp_cd": cd,
        "rsp_msg": msg,
        **{k: out.get(k) for k in PRICE_FIELDS if k in out},
    }


def fetch_investor(code: str) -> dict:
    data = call(
        "/krstock/quote/v1/currentInvestor",
        {"iem_cd": code, "market_cd": INVESTOR_MARKET, "array_cnt": str(INVESTOR_DAYS)},
    )
    cd, msg = status_of(data)
    rows = _block(data) or []
    if isinstance(rows, dict):
        rows = [rows]
    today = datetime.now().strftime("%Y%m%d")
    days = []
    for r in rows:
        days.append({
            "date": r.get("bsop_date1"),
            "provisional": r.get("bsop_date1") == today,   # 오늘 행 = 장중 잠정치(천 주 단위 반올림)
            "close": r.get("stck_prpr"),
            "chg_rate_abs": r.get("prdy_ctrt"),            # API 가 부호 없이 내려줌
            "volume": r.get("acml_vol"),
            # 실측 검증(2026-09-29): invest 가 거래소 공식 외국인 순매수와 일치 → 기본값으로 사용
            "foreign_net": r.get("invest"),
            # frgn_ntby_qty 는 공식치와 다름(당일은 0). 참고용으로만 보관
            "foreign_net_alt": r.get("frgn_ntby_qty"),
            "institution_net": r.get("gigwan"),
            "individual_net": r.get("person"),
            "program_net": r.get("program"),
            "foreign_ratio": r.get("for_rate"),
        })
    days.sort(key=lambda d: d.get("date") or "", reverse=True)   # 최신일 먼저
    # 등락 부호가 비어 오므로 전일 종가와 비교해 부호 있는 등락률을 직접 계산
    for i, d in enumerate(days[:-1]):
        c, pc = _num(d.get("close")), _num(days[i + 1].get("close"))
        if c and pc:
            d["chg_rate"] = round((c / pc - 1) * 100, 2)
    return {"market": INVESTOR_MARKET, "rsp_cd": cd, "rsp_msg": msg, "days": days}


def summarize_flow(days: list[dict]) -> dict:
    """매도신호 1·2번 점검용 집계. 해석(신호 판정)은 하지 않고 숫자만 만든다.
    오늘(잠정치) 포함 집계와 확정치만 쓴 집계를 둘 다 만든다."""
    def agg(rows):
        vals = [(d.get("date"), _num(d.get("foreign_net")), _num(d.get("institution_net"))) for d in rows]
        vals = [v for v in vals if v[1] is not None]
        if not vals:
            return {}
        streak = 0
        for _, f, _ in vals:
            if f < 0:
                streak += 1
            else:
                break
        last5, prev5 = vals[:5], vals[5:10]
        return {
            "from_date": last5[-1][0],
            "to_date": vals[0][0],
            "foreign_sell_streak_days": streak,
            "foreign_5d_sum": sum(v[1] for v in last5),
            "foreign_prev5d_sum": sum(v[1] for v in prev5) if len(prev5) == 5 else None,
            "institution_5d_sum": sum(v[2] for v in last5 if v[2] is not None),
            "both_selling_latest": vals[0][1] < 0 and (vals[0][2] or 0) < 0,
        }
    confirmed = [d for d in days if not d.get("provisional")]
    return {"with_today_provisional": agg(days), "confirmed_only": agg(confirmed)}


def fetch_one(item: dict, now: datetime, errors: list[str]) -> dict:
    """HOLDINGS/WATCHLIST 공통: 시세+투자자별 수급을 조회해 하나의 종목 레코드를 만든다."""
    out = {**item, "price": {}, "investor": {}, "flow_summary": {}}
    for m in PRICE_MARKETS:
        try:
            out["price"][m] = fetch_price(item["code"], m)
        except NhplugError as e:
            errors.append(f"{item['name']} 시세({m}): {e}")
    try:
        inv = fetch_investor(item["code"])
        out["investor"] = inv
        out["flow_summary"] = summarize_flow(inv["days"])
    except NhplugError as e:
        errors.append(f"{item['name']} 투자자별: {e}")

    p = _num((out["price"].get("KRX") or out["price"].get("UNT") or {}).get("stck_prpr"))
    if p:
        # 보유 종목이면(avg 있음) 평균단가 대비 손익도 계산
        avg = item.get("avg")
        if avg:
            out["pnl_pct_vs_avg"] = round((p / avg - 1) * 100, 2)
        # 부호 있는 등락률: 직전 확정 거래일의 KRX 종가 대비
        prev = next((d for d in out["investor"].get("days", []) if not d.get("provisional")), None)
        pc = _num(prev.get("close")) if prev else None
        if pc:
            out["prev_close_krx"] = pc
            out["chg_vs_prev_close"] = p - pc
            out["chg_rate_vs_prev_close"] = round((p / pc - 1) * 100, 2)
    return out


def _print_line(item: dict):
    pr = item["price"].get("KRX") or item["price"].get("UNT") or {}
    fs = (item.get("flow_summary") or {}).get("with_today_provisional") or {}
    rate = item.get("chg_rate_vs_prev_close")
    rate_txt = f"{rate:+.2f}%" if rate is not None else "등락률 확인불가"
    if fs:
        print(f"- {item['name']}: {pr.get('stck_prpr')}원 ({rate_txt}) | "
              f"외국인 연속순매도 {fs.get('foreign_sell_streak_days')}일, "
              f"5일합 {fs.get('foreign_5d_sum'):+,.0f} / 기관 5일합 {fs.get('institution_5d_sum'):+,.0f} (오늘 잠정치 포함)")
    else:
        print(f"- {item['name']}: {pr.get('stck_prpr')}원 ({rate_txt}) | 수급 확인불가")


def main() -> int:
    now = datetime.now()
    result = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": "NH투자증권 Namuh PLUG OpenAPI",
        "stocks": [],
        "watchlist": [],
        "errors": [],
    }
    for h in HOLDINGS:
        result["stocks"].append(fetch_one(h, now, result["errors"]))
    for w in WATCHLIST:
        result["watchlist"].append(fetch_one(w, now, result["errors"]))

    HIST_DIR.mkdir(exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (OUT_DIR / "nh_data_latest.json").write_text(text, encoding="utf-8")
    (HIST_DIR / f"nh_data_{now:%Y%m%d_%H%M}.json").write_text(text, encoding="utf-8")

    # 콘솔 요약
    print(f"[{result['fetched_at']}] 저장 완료: {OUT_DIR / 'nh_data_latest.json'}")
    print("[보유 종목]")
    for s in result["stocks"]:
        _print_line(s)
    print("[관심종목 - 미보유]")
    for s in result["watchlist"]:
        _print_line(s)
    for e in result["errors"]:
        print("! 오류:", e)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.exit(main())
    except NhplugError as e:
        print(f"API 오류: {e}")
        if e.category in ("config", "auth"):
            print("→ C:\\Users\\<사용자>\\.nhplug\\.env 의 APP_KEY/SECRET 과 BASE_URL 을 확인하세요.")
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(3)
