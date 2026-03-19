"""
KIS Client – 한국투자증권 오픈 API 클라이언트

실전/모의 투자 공통 지원.
환경변수 KIS_MOCK=true 이면 모의투자 도메인을 사용합니다.

공식 문서: https://apiportal.koreainvestment.com
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


# ── 토큰 캐시 ─────────────────────────────────────────────────────────────────
_access_token: Optional[str] = None
_token_expires: datetime = datetime.min


async def get_access_token() -> str:
    """OAuth2 Access Token을 발급/캐싱합니다."""
    global _access_token, _token_expires

    if _access_token and datetime.utcnow() < _token_expires:
        return _access_token

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
            "eval_amount":   int(item.get("evlu_amt", 0)),
            "profit_loss":   float(item.get("evlu_pfls_rt", 0)),
        }
        for item in output1
        if int(item.get("hldg_qty", 0)) > 0
    ]

    return {
        "total_eval":    int(summary.get("tot_evlu_amt", 0)),       # 총 평가금액
        "available_cash": int(summary.get("nxdy_excc_amt", 0)),     # 익일 예수금
        "total_profit":  float(summary.get("evlu_pfls_smtl_amt", 0)),
        "holdings":      holdings,
    }


# ── 매수 주문 ──────────────────────────────────────────────────────────────────

async def buy_order(code: str, quantity: int, price: int = 0, order_type: str = "01") -> dict:
    """
    매수 주문
    order_type: "00" = 지정가, "01" = 시장가
    TR: TTTC0802U (실전) / VTTC0802U (모의)
    """
    tr_id = "VTTC0802U" if IS_MOCK else "TTTC0802U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body  = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO":         code,
        "ORD_DVSN":     order_type,          # 00: 지정가, 01: 시장가
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     str(price) if order_type == "00" else "0",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    output = data.get("output", {})
    rt_cd  = data.get("rt_cd", "9")

    return {
        "success":    rt_cd == "0",
        "order_no":   output.get("odno", ""),
        "order_time": output.get("ord_tmd", ""),
        "message":    data.get("msg1", ""),
        "code":       code,
        "quantity":   quantity,
        "price":      price,
        "order_type": "BUY",
        "mock":       IS_MOCK,
    }


# ── 매도 주문 ──────────────────────────────────────────────────────────────────

async def sell_order(code: str, quantity: int, price: int = 0, order_type: str = "01") -> dict:
    """
    매도 주문
    TR: TTTC0801U (실전) / VTTC0801U (모의)
    """
    tr_id = "VTTC0801U" if IS_MOCK else "TTTC0801U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body  = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO":         code,
        "ORD_DVSN":     order_type,
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     str(price) if order_type == "00" else "0",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    output = data.get("output", {})
    rt_cd  = data.get("rt_cd", "9")

    return {
        "success":    rt_cd == "0",
        "order_no":   output.get("odno", ""),
        "order_time": output.get("ord_tmd", ""),
        "message":    data.get("msg1", ""),
        "code":       code,
        "quantity":   quantity,
        "price":      price,
        "order_type": "SELL",
        "mock":       IS_MOCK,
    }


# ── 주문 취소 ──────────────────────────────────────────────────────────────────

async def cancel_order(org_order_no: str, code: str, quantity: int) -> dict:
    """
    주문 취소
    TR: TTTC0803U (실전) / VTTC0803U (모의)
    """
    tr_id = "VTTC0803U" if IS_MOCK else "TTTC0803U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body  = {
        "CANO":           CANO,
        "ACNT_PRDT_CD":   ACNT_PRDT_CD,
        "KRX_FWDG_ORD_ORGNO": "",
        "ORGN_ODNO":      org_order_no,
        "ORD_DVSN":       "00",
        "RVSE_CNCL_DVSN_CD": "02",       # 02 = 취소
        "ORD_QTY":        str(quantity),
        "ORD_UNPR":       "0",
        "QTY_ALL_ORD_YN": "Y",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, headers=await _headers(tr_id), json=body)
        res.raise_for_status()
        data = res.json()

    return {
        "success": data.get("rt_cd") == "0",
        "message": data.get("msg1", ""),
    }
