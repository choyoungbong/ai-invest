"""
US KIS Client — 한국투자증권 해외 주식 API 클라이언트

국내 계좌와 해외 계좌를 완전히 분리하여 관리합니다.
- 해외 계좌번호: KIS_OVERSEAS_ACCOUNT_NO (별도 .env 설정)
- API 키: 국내와 동일 키 재사용 (KIS 정책상 1계정 1키)

KIS 해외 주요 TR_ID:
  매수: TTTT1002U (실전) / VTTT1002U (모의)
  매도: TTTT1006U (실전) / VTTT1006U (모의)
  잔고: TTTS3012R (실전) / VTTS3012R (모의)
  현재가: HHDFS00000300
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

import httpx
import pytz

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# ── 환경변수 ──────────────────────────────────────────────────────────────────
# 해외 전용 키 (KIS_OVERSEAS_APP_KEY 없으면 국내 키 fallback)
APP_KEY    = os.getenv("KIS_OVERSEAS_APP_KEY") or os.getenv("KIS_APP_KEY",    "")
APP_SECRET = os.getenv("KIS_OVERSEAS_APP_SECRET") or os.getenv("KIS_APP_SECRET", "")
IS_MOCK    = os.getenv("KIS_MOCK", "false").lower() == "true"

# 해외 전용 계좌번호 (국내와 별도)
OVERSEAS_ACCOUNT_NO = os.getenv("KIS_OVERSEAS_ACCOUNT_NO", "")

US_TRADING_ENABLED = os.getenv("US_TRADING_ENABLED", "false").lower() == "true"

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443" if IS_MOCK
    else "https://openapi.koreainvestment.com:9443"
)

# 대상 ETF 거래소 코드
EXCHANGE_MAP = {
    # 나스닥 개별주 (모두 NAS)
    "MARA": "NAS", "JOBY": "NAS", "GRAB": "NAS", "OPEN": "NAS",
    "CLOV": "NAS", "SOFI": "NAS", "RIVN": "NAS", "DKNG": "NAS",
    "CHWY": "NAS", "SNAP": "NAS", "LCID": "NAS", "PLUG": "NAS",
    # 기존 ETF (하위 호환)
    "SPLG": "NYS", "TQQQ": "NAS", "SOXL": "AMS",
}

# 토큰 캐시
_token: str | None = None
_token_expires: datetime | None = None
_token_lock = asyncio.Lock()


def _parse_account(account_no: str) -> tuple[str, str]:
    """계좌번호 파싱: '12345678-01' → ('12345678', '01')"""
    clean = account_no.replace("-", "")
    if len(clean) < 10:
        raise ValueError(f"해외 계좌번호 형식 오류: {account_no} (예: 12345678-01)")
    return clean[:8], clean[8:10]


async def _get_token() -> str:
    """토큰 발급/갱신 (만료 10분 전 자동 갱신)"""
    global _token, _token_expires

    async with _token_lock:
        now = datetime.utcnow()
        if _token and _token_expires and now < _token_expires - timedelta(minutes=10):
            return _token

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{BASE_URL}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey":     APP_KEY,
                    "appsecret":  APP_SECRET,
                },
            )
            data = resp.json()

        if "access_token" not in data:
            raise RuntimeError(f"토큰 발급 실패: {data}")

        _token         = data["access_token"]
        _token_expires = datetime.utcnow() + timedelta(seconds=int(data.get("expires_in", 86400)))
        logger.info("[US_KIS] 토큰 발급 완료")
        return _token


# ── 현재가 조회 ───────────────────────────────────────────────────────────────

async def get_us_price(symbol: str) -> dict:
    """
    미국 주식 현재가 조회.

    Returns:
        {"symbol": str, "price": float, "change_rate": float,
         "volume": int, "exchange": str}
    """
    token    = await _get_token()
    exchange = EXCHANGE_MAP.get(symbol, "NAS")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/uapi/overseas-price/v1/quotations/price",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        APP_KEY,
                "appsecret":     APP_SECRET,
                "tr_id":         "HHDFS00000300",
            },
            params={"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        data = resp.json()

    out   = data.get("output", {})
    price = float(out.get("last", 0) or 0)

    return {
        "symbol":      symbol,
        "price":       price,
        "change_rate": float(out.get("rate", 0) or 0),
        "volume":      int(out.get("tvol", 0) or 0),
        "exchange":    exchange,
    }


# ── 잔고 조회 ─────────────────────────────────────────────────────────────────

async def get_us_balance() -> dict:
    """
    해외 계좌 잔고 전체 조회.

    Returns:
        {
            "cash_usd": float,
            "total_usd": float,
            "holdings": [
                {"symbol": str, "name": str, "quantity": int,
                 "avg_price": float, "current_price": float,
                 "pnl_pct": float, "exchange": str}
            ]
        }
    """
    if not OVERSEAS_ACCOUNT_NO:
        raise RuntimeError(
            "KIS_OVERSEAS_ACCOUNT_NO 미설정. .env에 해외 계좌번호를 입력하세요."
        )

    token          = await _get_token()
    acc_no, acc_cd = _parse_account(OVERSEAS_ACCOUNT_NO)
    tr_id          = "VTTS3012R" if IS_MOCK else "TTTS3012R"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        APP_KEY,
                "appsecret":     APP_SECRET,
                "tr_id":         tr_id,
            },
            params={
                "CANO":           acc_no,
                "ACNT_PRDT_CD":   acc_cd,
                "OVRS_EXCG_CD":   "NAS",
                "TR_CRCY_CD":     "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        data = resp.json()

    out1 = data.get("output1", [])
    out2_raw = data.get("output2", {})
    # output2가 리스트인 경우 처리
    if isinstance(out2_raw, list):
        out2 = out2_raw[0] if out2_raw else {}
    else:
        out2 = out2_raw

    holdings = []
    for item in out1:
        qty = int(item.get("cblc_qty", 0) or 0)
        if qty <= 0:
            continue
        holdings.append({
            "symbol":        item.get("ovrs_pdno", ""),
            "name":          item.get("ovrs_item_name", ""),
            "quantity":      qty,
            "avg_price":     float(item.get("pchs_avg_pric", 0) or 0),
            "current_price": float(item.get("now_pric2", 0) or 0),
            "pnl_pct":       float(item.get("evlu_pfls_rt", 0) or 0),
            "exchange":      item.get("ovrs_excg_cd", ""),
        })

    # 외화 예수금 별도 조회 (CTRP6504R)
    cash_usd = 0.0
    try:
        async with httpx.AsyncClient(timeout=15) as client2:
            resp2 = await client2.get(
                f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-present-balance",
                headers={
                    "authorization": f"Bearer {token}",
                    "appkey":        APP_KEY,
                    "appsecret":     APP_SECRET,
                    "tr_id":         "CTRP6504R",
                },
                params={
                    "CANO":             acc_no,
                    "ACNT_PRDT_CD":     acc_cd,
                    "WCRC_FRCR_DVSN_CD": "02",
                    "NATN_CD":          "840",
                    "TR_MKET_CD":       "NY",
                    "INQR_DVSN_CD":     "00",
                },
            )
            data2 = resp2.json()
            out2b = data2.get("output2", [])
            if isinstance(out2b, list) and out2b:
                for item in out2b:
                    if item.get("crcy_cd") == "USD":
                        cash_usd = float(item.get("frcr_dncl_amt_2", 0) or 0)
                        break
    except Exception as e:
        logger.warning(f"[US_KIS] 외화 예수금 조회 실패: {e}")
    total_usd = cash_usd + sum(
        float(h["current_price"]) * h["quantity"] for h in holdings
    )

    logger.info(
        f"[US_KIS] 잔고 — 예수금 ${cash_usd:.2f} / "
        f"총자산 ${total_usd:.2f} / {len(holdings)}종목"
    )
    return {"cash_usd": cash_usd, "total_usd": total_usd, "holdings": holdings}


# ── 주문 실행 ─────────────────────────────────────────────────────────────────

async def _order(symbol: str, quantity: int, side: str, price: float = 0) -> dict:
    """
    해외 주식 매수/매도 주문.
    price=0 이면 시장가 상당 주문 (지정가 0원 처리).
    """
    if not OVERSEAS_ACCOUNT_NO:
        raise RuntimeError("KIS_OVERSEAS_ACCOUNT_NO 미설정")

    token          = await _get_token()
    acc_no, acc_cd = _parse_account(OVERSEAS_ACCOUNT_NO)
    exchange       = EXCHANGE_MAP.get(symbol, "NAS")

    tr_map = {
        ("BUY",  True):  "VTTT1002U",
        ("BUY",  False): "TTTT1002U",
        ("SELL", True):  "VTTT1006U",
        ("SELL", False): "TTTT1006U",
    }
    tr_id = tr_map[(side, IS_MOCK)]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/order",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        APP_KEY,
                "appsecret":     APP_SECRET,
                "tr_id":         tr_id,
                "custtype":      "P",
            },
            json={
                "CANO":             acc_no,
                "ACNT_PRDT_CD":     acc_cd,
                "OVRS_EXCG_CD":     exchange,
                "PDNO":             symbol,
                "ORD_DVSN":         "00",
                "ORD_QTY":          str(quantity),
                "OVRS_ORD_UNPR":    f"{price:.2f}" if price > 0 else "0",
                "ORD_SVR_DVSN_CD":  "0",
            },
        )
        data = resp.json()

    success  = data.get("rt_cd") == "0"
    order_no = data.get("output", {}).get("odno", "")
    message  = data.get("msg1", "")

    if success:
        logger.info(f"[US_KIS] {side} 성공: {symbol} {quantity}주 (주문번호: {order_no})")
    else:
        logger.error(f"[US_KIS] {side} 실패: {symbol} — {message}")

    return {"success": success, "order_no": order_no, "message": message}


async def us_buy_order(symbol: str, quantity: int, price: float = 0) -> dict:
    return await _order(symbol, quantity, "BUY", price)


async def us_sell_order(symbol: str, quantity: int, price: float = 0) -> dict:
    return await _order(symbol, quantity, "SELL", price)


# ── 비대상 종목 자동 청산 ─────────────────────────────────────────────────────

async def liquidate_non_target_holdings(target_symbols: list[str]) -> list[dict]:
    """
    매매 대상이 아닌 기존 보유 종목을 전량 시장가 매도합니다.
    미국장 시작 시 자동으로 호출됩니다.

    target_symbols: 자동매매 대상 ETF 목록 (이 종목들은 청산하지 않음)
    """
    balance  = await get_us_balance()
    holdings = balance.get("holdings", [])

    non_target = [h for h in holdings if h["symbol"] not in target_symbols]

    if not non_target:
        logger.info("[US_KIS] 청산할 비대상 종목 없음")
        return []

    logger.warning(
        f"[US_KIS] 비대상 종목 청산 시작: "
        f"{[h['symbol'] for h in non_target]}"
    )

    results = []
    for h in non_target:
        symbol = h["symbol"]
        qty    = h["quantity"]

        result = await us_sell_order(symbol, qty)
        results.append({
            "symbol":    symbol,
            "name":      h["name"],
            "quantity":  qty,
            "avg_price": h["avg_price"],
            "pnl_pct":   h["pnl_pct"],
            "success":   result["success"],
            "order_no":  result["order_no"],
            "message":   result["message"],
        })

        logger.info(
            f"[US_KIS] 청산: {symbol} {qty}주 "
            f"(평균단가 ${h['avg_price']:.2f} / 손익 {h['pnl_pct']:+.1f}%)"
        )
        await asyncio.sleep(0.5)

    success_cnt = sum(1 for r in results if r["success"])
    logger.info(f"[US_KIS] 청산 완료: {success_cnt}/{len(results)}건 성공")
    return results


# ── 미국장 시간 판단 ──────────────────────────────────────────────────────────

def is_us_market_open() -> bool:
    """
    미국 정규장 운영 시간 여부 (KST 기준).
    실제 매매: 22:35~04:50 KST (개장 5분 후 ~ 마감 10분 전)
    서머타임 미적용 기준 (서머타임 시 1시간 앞당겨짐)
    """
    now     = datetime.now(KST)
    weekday = now.weekday()   # 0=월 ~ 6=일
    hour    = now.hour
    minute  = now.minute

    # 22:35~23:59: 월~금
    night = (weekday in range(0, 5)) and (
        (hour == 22 and minute >= 35) or hour == 23
    )
    # 00:00~04:50: 화~토 (전날 밤 연장)
    dawn = (weekday in range(1, 6)) and (
        hour in range(0, 4) or (hour == 4 and minute <= 50)
    )

    return night or dawn
