"""
KIS API 연동 검증 FastAPI 라우터

/kis-verify/* 엔드포인트를 통해 브라우저/Swagger에서도 검증할 수 있습니다.
"""
import os
from fastapi import APIRouter
import httpx

router = APIRouter(prefix="/kis-verify", tags=["KIS 연동 검증"])

APP_KEY    = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
IS_MOCK    = os.getenv("KIS_MOCK", "true").lower() == "true"
BASE_URL   = "https://openapivts.koreainvestment.com:29443" if IS_MOCK else "https://openapi.koreainvestment.com:9443"

_acc   = ACCOUNT_NO.split("-") if ACCOUNT_NO else ["", ""]
CANO   = _acc[0]
ACNT_PRDT_CD = _acc[1] if len(_acc) > 1 else "01"


@router.get("/status")
async def verify_status():
    """현재 KIS 설정 상태 확인"""
    return {
        "mode":        "모의투자" if IS_MOCK else "실전투자",
        "base_url":    BASE_URL,
        "app_key_set":    bool(APP_KEY),
        "app_secret_set": bool(APP_SECRET),
        "account_no":  ACCOUNT_NO,
        "cano":        CANO,
        "acnt_prdt_cd": ACNT_PRDT_CD,
    }


@router.post("/token")
async def verify_token():
    """STEP 3: Access Token 발급 테스트"""
    if not APP_KEY or not APP_SECRET:
        return {"success": False, "error": "KIS_APP_KEY 또는 KIS_APP_SECRET 미설정"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{BASE_URL}/oauth2/tokenP",
                json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
            )
        data = res.json()
        if res.status_code == 200 and "access_token" in data:
            token = data["access_token"]
            return {
                "success":    True,
                "token_preview": token[:20] + "...",
                "expires_in": data.get("expires_in"),
            }
        return {"success": False, "status_code": res.status_code, "response": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/price/{code}")
async def verify_price(code: str = "005930"):
    """STEP 4: 현재가 조회 테스트"""
    try:
        from trader.kis_client import get_access_token, get_current_price
        await get_access_token()
        result = await get_current_price(code)
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/balance")
async def verify_balance():
    """STEP 5: 잔고 조회 테스트"""
    try:
        from trader.kis_client import get_access_token, get_balance
        await get_access_token()
        result = await get_balance()
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/order-test")
async def verify_order():
    """STEP 6: 모의 매수 주문 테스트 (모의투자 모드에서만 실행)"""
    if not IS_MOCK:
        return {"success": False, "error": "실전 투자 모드에서는 테스트 주문을 실행하지 않습니다. KIS_MOCK=true 로 설정하세요."}
    try:
        from trader.kis_client import get_access_token, buy_order
        await get_access_token()
        result = await buy_order("005930", 1, 0, "01")  # 삼성전자 1주 시장가
        return {"success": result["success"], "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/full-check")
async def full_verify():
    """STEP 1~5 전체 연동 검증 (주문 제외)"""
    report = {}

    # 환경변수
    report["env"] = {
        "success": bool(APP_KEY and APP_SECRET and ACCOUNT_NO),
        "app_key_set":    bool(APP_KEY),
        "app_secret_set": bool(APP_SECRET),
        "account_set":    bool(ACCOUNT_NO),
    }

    # 네트워크
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(f"{BASE_URL}/oauth2/tokenP")
        report["network"] = {"success": res.status_code in [200,400,401,403,405]}
    except Exception as e:
        report["network"] = {"success": False, "error": str(e)}

    # 토큰
    try:
        from trader.kis_client import get_access_token, _access_token
        import trader.kis_client as kc
        kc._access_token = None  # 강제 재발급
        token = await get_access_token()
        report["token"] = {"success": bool(token)}
    except Exception as e:
        report["token"] = {"success": False, "error": str(e)}
        return {"overall": False, "report": report}

    # 현재가
    try:
        from trader.kis_client import get_current_price
        price = await get_current_price("005930")
        report["price"] = {"success": price["price"] > 0, "price": price["price"]}
    except Exception as e:
        report["price"] = {"success": False, "error": str(e)}

    # 잔고
    try:
        from trader.kis_client import get_balance
        bal = await get_balance()
        report["balance"] = {"success": True, "available_cash": bal["available_cash"]}
    except Exception as e:
        report["balance"] = {"success": False, "error": str(e)}

    overall = all(v.get("success", False) for v in report.values())
    return {"overall": overall, "mode": "모의투자" if IS_MOCK else "실전투자", "report": report}
