"""
KIS Client – 한국투자증권 오픈 API 클라이언트

실전/모의 투자 공통 지원.
환경변수 KIS_MOCK=true 이면 모의투자 도메인을 사용합니다.

공식 문서: https://apiportal.koreainvestment.com

[버그 수정 v2]
  - KIS 토큰 발급 Race Condition 수정
    기존: _access_token이 전역 변수로 관리되며 asyncio.Lock 없음
          → 손절/익절(5분) + 2차분할매수(10분) + 스캔 job이 동시에 토큰 만료를
            감지하면 중복 발급 요청 발생
          → KIS는 동일 계정 중복 발급 시 기존 토큰 무효화 → 진행 중인 API 호출 실패
    수정: _token_lock (asyncio.Lock) 추가 + double-checked locking 패턴 적용
          → 첫 번째 호출만 토큰 발급, 나머지는 대기 후 캐시 재사용
"""
import logging
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────────────────────────
APP_KEY    = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")   # 예) 12345678-01
IS_MOCK    = os.getenv("KIS_MOCK", "true").lower() == "true"

REAL_BASE  = "https://openapi.koreainvestment.com:9443"
MOCK_BASE  = "https://openapivts.koreainvestment.com:29443"
BASE_URL   = MOCK_BASE if IS_MOCK else REAL_BASE

# 계좌번호 분리
_acc_parts  = ACCOUNT_NO.split("-") if ACCOUNT_NO else ["", ""]
CANO        = _acc_parts[0]           # 계좌번호 앞 8자리
ACNT_PRDT_CD = _acc_parts[1] if len(_acc_parts) > 1 else "01"


# ── 토큰 캐시 + Lock ──────────────────────────────────────────────────────────
# [버그 수정] asyncio.Lock 추가
#   기존: Lock 없이 전역 변수로만 관리
#         → 여러 비동기 job이 동시에 토큰 만료를 감지하면 중복 발급 요청
#         → KIS는 중복 발급 시 기존 토큰 무효화 → 진행 중 API 호출 401 에러
#   수정: _token_lock으로 발급 직렬화 + double-checked locking 패턴
#         → Lock 획득 후 다시 한번 캐시 유효성 검사
#         → 첫 번째 코루틴만 실제 발급, 대기 중이던 나머지는 캐시 재사용
_access_token: Optional[str] = None
_token_expires: datetime = datetime.min
_token_lock: asyncio.Lock = asyncio.Lock()


async def get_access_token() -> str:
    """
    OAuth2 Access Token을 발급/캐싱합니다.

    [버그 수정] double-checked locking 패턴으로 Race Condition 방지:
      1차 검사: Lock 획득 전 빠른 캐시 확인 (유효하면 Lock 없이 즉시 반환)
      Lock 획득: 만료된 경우에만 진입
      2차 검사: Lock 획득 후 다시 캐시 확인 (대기 중에 다른 코루틴이 갱신했을 수 있음)
      발급: 2차 검사도 만료인 경우에만 실제 API 호출
    """
    global _access_token, _token_expires

    # 1차 검사: Lock 없이 빠른 경로 (대부분의 호출이 여기서 반환됨)
    if _access_token and datetime.utcnow() < _token_expires:
        return _access_token

    # [버그 수정] Lock 획득 후 직렬 처리
    async with _token_lock:
        # 2차 검사 (double-check): Lock 대기 중에 다른 코루틴이 이미 갱신했을 수 있음
        if _access_token and datetime.utcnow() < _token_expires:
            return _access_token

        # 실제 토큰 발급
        if not APP_KEY or not APP_SECRET:
            raise ValueError("KIS_APP_KEY / KIS_APP_SECRET 환경변수가 설정되지 않았습니다.")

        url  = f"{BASE_URL}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     APP_KEY,
            "appsecret":  APP_SECRET,
        }

        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(url, json=body)
            res.raise_for_status()
            data = res.json()

        _access_token  = data["access_token"]
        _token_expires = datetime.utcnow() + timedelta(seconds=int(data.get("expires_in", 82800)) - 300)
        logger.info(f"KIS 토큰 발급 완료 ({'모의' if IS_MOCK else '실전'}투자)")
        return _access_token


async def _headers(tr_id: str) -> dict:
    token = await get_access_token()
    return {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey":        APP_KEY,
        "appsecret":     APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }


# ── 현재가 조회 ────────────────────────────────────────────────────────────────

async def get_current_price(code: str) -> dict:
    """
    주식 현재가 조회
    TR: FHKST01010100
    """
    tr_id = "FHKST01010100"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         code,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url, headers=await _headers(tr_id), params=params)
        res.raise_for_status()
        data = res.json()

    output = data.get("output", {})
    return {
        "code":        code,
        "price":       int(output.get("stck_prpr", 0)),      # 현재가
        "open":        int(output.get("stck_oprc", 0)),      # 시가
        "high":        int(output.get("stck_hgpr", 0)),      # 고가
        "low":         int(output.get("stck_lwpr", 0)),      # 저가
        "volume":      int(output.get("acml_vol", 0)),        # 누적거래량
        "change_rate": float(output.get("prdy_ctrt", 0)),    # 전일대비율
        "per":         float(output.get("per", 0)),
        "pbr":         float(output.get("pbr", 0)),
    }


# ── 잔고 조회 ──────────────────────────────────────────────────────────────────

async def get_balance() -> dict:
    """
    주식 잔고 조회
    TR: TTTC8434R (실전) / VTTC8434R (모의)
    """
    tr_id = "VTTC8434R" if IS_MOCK else "TTTC8434R"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO":            CANO,
        "ACNT_PRDT_CD":    ACNT_PRDT_CD,
        "AFHR_FLPR_YN":   "N",
        "OFL_YN":         "",
        "INQR_DVSN":      "02",
        "UNPR_DVSN":      "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN":      "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url, headers=await _headers(tr_id), params=params)
        res.raise_for_status()
        data = res.json()

    output1 = data.get("output1", [])   # 보유 종목 목록
    output2 = data.get("output2", [{}])  # 계좌 요약

    summary = output2[0] if output2 else {}
    holdings = [
        {
            "code":          item.get("pdno", ""),
            "name":          item.get("prdt_name", ""),
            "quantity":      int(item.get("hldg_qty", 0)),
            "avg_price":     float(item.get("pchs_avg_pric", 0)),
            "current_price": int(item.get("prpr", 0)),
            "profit_loss":   float(item.get("evlu_pfls_amt", 0)),
            "profit_pct":    float(item.get("evlu_pfls_rt", 0)),
        }
        for item in output1
        if int(item.get("hldg_qty", 0)) > 0
    ]

    return {
        "holdings":        holdings,
        "total_eval":      int(summary.get("tot_evlu_amt", 0)),
        "available_cash":  int(summary.get("dnca_tot_amt", 0)),
        "total_profit":    float(summary.get("evlu_pfls_smtl_amt", 0)),
    }


# ── 매수 주문 ──────────────────────────────────────────────────────────────────

async def buy_order(code: str, quantity: int, order_type: str = "01") -> dict:
    """
    주식 매수 주문
    TR: TTTC0802U (실전) / VTTC0802U (모의)
    order_type: "01" 시장가, "00" 지정가
    """
    tr_id = "VTTC0802U" if IS_MOCK else "TTTC0802U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body  = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO":         code,
        "ORD_DVSN":     order_type,
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     "0",  # 시장가는 0
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    success  = data.get("rt_cd") == "0"
    order_no = data.get("output", {}).get("ODNO", "")

    if not success:
        logger.error(f"매수 주문 실패 [{code}]: {data.get('msg1', '')}")

    return {
        "success":  success,
        "order_no": order_no,
        "message":  data.get("msg1", ""),
        "code":     data.get("rt_cd", ""),
    }


# ── 매도 주문 ──────────────────────────────────────────────────────────────────

async def sell_order(code: str, quantity: int, order_type: str = "01") -> dict:
    """
    주식 매도 주문
    TR: TTTC0801U (실전) / VTTC0801U (모의)
    order_type: "01" 시장가, "00" 지정가
    """
    tr_id = "VTTC0801U" if IS_MOCK else "TTTC0801U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body  = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO":         code,
        "ORD_DVSN":     order_type,
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     "0",  # 시장가는 0
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    success  = data.get("rt_cd") == "0"
    order_no = data.get("output", {}).get("ODNO", "")

    if not success:
        logger.error(f"매도 주문 실패 [{code}]: {data.get('msg1', '')}")

    return {
        "success":  success,
        "order_no": order_no,
        "message":  data.get("msg1", ""),
        "code":     data.get("rt_cd", ""),
    }


# ── 주문 취소 ──────────────────────────────────────────────────────────────────

async def cancel_order(order_no: str, code: str, quantity: int) -> dict:
    """
    주문 취소
    TR: TTTC0803U (실전) / VTTC0803U (모의)
    """
    tr_id = "VTTC0803U" if IS_MOCK else "TTTC0803U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body  = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "KRX_FWDG_ORD_ORGNO": "",
        "ORGN_ODNO":    order_no,
        "ORD_DVSN":     "02",
        "RVSE_CNCL_DVSN_CD": "02",  # 취소
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     "0",
        "PDNO":         code,
        "QTY_ALL_ORD_YN": "Y",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    success = data.get("rt_cd") == "0"
    return {
        "success": success,
        "message": data.get("msg1", ""),
    }


# ── 일별 거래 내역 조회 ────────────────────────────────────────────────────────

async def get_daily_trades(date_str: str = "") -> dict:
    """
    일별 매매 내역 조회
    TR: TTTC8001R (실전) / VTTC8001R (모의)
    date_str: "YYYYMMDD" 형식, 빈 값이면 오늘
    """
    tr_id = "VTTC8001R" if IS_MOCK else "TTTC8001R"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    params = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "INQR_STRT_DT": date_str,
        "INQR_END_DT":  date_str,
        "SLL_BUY_DVSN_CD": "00",  # 전체
        "INQR_DVSN":    "00",
        "PDNO":         "",
        "CCLD_DVSN":    "01",     # 체결
        "ORD_GNO_BRNO": "",
        "ODNO":         "",
        "INQR_DVSN_3":  "00",
        "INQR_DVSN_1":  "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url, headers=await _headers(tr_id), params=params)
        res.raise_for_status()
        data = res.json()

    trades = [
        {
            "code":       item.get("pdno", ""),
            "name":       item.get("prdt_name", ""),
            "order_type": "BUY" if item.get("sll_buy_dvsn_cd") == "02" else "SELL",
            "quantity":   int(item.get("ord_qty", 0)),
            "price":      int(item.get("avg_prvs", 0)),
            "amount":     int(item.get("pchs_amt", 0)),
            "order_no":   item.get("odno", ""),
            "order_time": item.get("ord_tmd", ""),
        }
        for item in data.get("output1", [])
    ]

    return {"trades": trades, "date": date_str}
