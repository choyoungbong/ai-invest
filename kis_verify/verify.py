"""
KIS API 단계별 연동 검증 스크립트

실행 방법:
  python kis_verify/verify.py

STEP 1 ~ 6 을 순서대로 실행하며 각 단계의 성공/실패를 출력합니다.
.env 파일이 프로젝트 루트에 있어야 합니다.
"""
import asyncio
import os
import sys
import json
from datetime import datetime

# 루트 경로를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import httpx

# ── 설정 로드 ──────────────────────────────────────────────────────────────────
APP_KEY    = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
IS_MOCK    = os.getenv("KIS_MOCK", "true").lower() == "true"

REAL_BASE = "https://openapi.koreainvestment.com:9443"
MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
BASE_URL  = MOCK_BASE if IS_MOCK else REAL_BASE

_acc_parts   = ACCOUNT_NO.split("-") if ACCOUNT_NO else ["", ""]
CANO         = _acc_parts[0]
ACNT_PRDT_CD = _acc_parts[1] if len(_acc_parts) > 1 else "01"

MODE_LABEL = "🟡 모의투자" if IS_MOCK else "🔴 실전투자"

# ── 출력 유틸 ──────────────────────────────────────────────────────────────────
def ok(msg):  print(f"  ✅ {msg}")
def fail(msg): print(f"  ❌ {msg}")
def info(msg): print(f"  ℹ️  {msg}")
def sep():     print("─" * 55)


# ── STEP 1: 환경변수 확인 ──────────────────────────────────────────────────────
def step1_check_env():
    print("\n[STEP 1] 환경변수 설정 확인")
    sep()
    passed = True

    checks = [
        ("KIS_APP_KEY",    APP_KEY,    "AppKey"),
        ("KIS_APP_SECRET", APP_SECRET, "AppSecret"),
        ("KIS_ACCOUNT_NO", ACCOUNT_NO, "계좌번호 (예: 12345678-01)"),
    ]
    for env_name, val, label in checks:
        if val:
            masked = val[:6] + "****" + val[-4:] if len(val) > 10 else "****"
            ok(f"{env_name} = {masked}  ({label})")
        else:
            fail(f"{env_name} 미설정 — .env 파일에 {env_name}={label} 을 추가하세요")
            passed = False

    info(f"투자 모드: {MODE_LABEL}")
    info(f"API 도메인: {BASE_URL}")
    info(f"계좌번호: CANO={CANO}, ACNT_PRDT_CD={ACNT_PRDT_CD}")
    return passed


# ── STEP 2: 네트워크 연결 확인 ─────────────────────────────────────────────────
async def step2_check_network():
    print("\n[STEP 2] KIS 서버 네트워크 연결 확인")
    sep()
    host = "openapivts.koreainvestment.com" if IS_MOCK else "openapi.koreainvestment.com"
    port = 29443 if IS_MOCK else 9443

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(f"{BASE_URL}/oauth2/tokenP", timeout=5)
            # 405 Method Not Allowed = 서버 응답 있음 (GET으로 POST 엔드포인트 호출)
            if res.status_code in [200, 400, 401, 403, 405]:
                ok(f"KIS 서버 응답 확인 (status={res.status_code})")
                return True
            else:
                fail(f"예상치 못한 응답코드: {res.status_code}")
                return False
    except httpx.ConnectError:
        fail(f"연결 실패 — {host}:{port} 에 도달할 수 없습니다")
        info("방화벽 또는 포트 차단 여부를 확인하세요")
        return False
    except httpx.TimeoutException:
        fail("연결 타임아웃 (5초)")
        return False
    except Exception as e:
        fail(f"네트워크 오류: {e}")
        return False


# ── STEP 3: Access Token 발급 ─────────────────────────────────────────────────
async def step3_get_token():
    print("\n[STEP 3] Access Token 발급")
    sep()

    url  = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     APP_KEY,
        "appsecret":  APP_SECRET,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(url, json=body)

        data = res.json()

        if res.status_code == 200 and "access_token" in data:
            token = data["access_token"]
            expires = data.get("expires_in", "unknown")
            ok(f"토큰 발급 성공")
            ok(f"토큰 앞 20자: {token[:20]}...")
            ok(f"만료시간: {expires}초 ({int(expires)//3600}시간)")
            return token
        else:
            fail(f"토큰 발급 실패 (HTTP {res.status_code})")
            info(f"응답: {json.dumps(data, ensure_ascii=False, indent=2)[:300]}")

            # 오류 원인 안내
            if res.status_code == 401:
                info("→ APP_KEY 또는 APP_SECRET 이 잘못되었습니다")
            elif res.status_code == 403:
                info("→ API 사용 권한이 없습니다. KIS 포털에서 앱 상태를 확인하세요")
            elif "error" in data:
                info(f"→ 오류 메시지: {data.get('error_description', data.get('error', ''))}")
            return None

    except Exception as e:
        fail(f"토큰 요청 실패: {e}")
        return None


# ── STEP 4: 현재가 조회 ────────────────────────────────────────────────────────
async def step4_get_price(token: str):
    print("\n[STEP 4] 현재가 조회 (삼성전자 005930)")
    sep()

    test_code = "005930"
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        APP_KEY,
        "appsecret":     APP_SECRET,
        "tr_id":         "FHKST01010100",
        "custtype":      "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         test_code,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers, params=params)

        data = res.json()

        if res.status_code == 200 and data.get("rt_cd") == "0":
            output = data.get("output", {})
            price  = int(output.get("stck_prpr", 0))
            chg    = float(output.get("prdy_ctrt", 0))
            vol    = int(output.get("acml_vol", 0))
            ok(f"현재가 조회 성공")
            ok(f"삼성전자(005930): {price:,}원  ({chg:+.2f}%)")
            ok(f"누적 거래량: {vol:,}주")
            return True
        else:
            fail(f"현재가 조회 실패 (HTTP {res.status_code})")
            info(f"rt_cd: {data.get('rt_cd')}  msg: {data.get('msg1', '')}")
            if data.get("rt_cd") == "1":
                info(f"→ {data.get('msg1', '')}")
            return False

    except Exception as e:
        fail(f"현재가 조회 오류: {e}")
        return False


# ── STEP 5: 잔고 조회 ─────────────────────────────────────────────────────────
async def step5_get_balance(token: str):
    print("\n[STEP 5] 계좌 잔고 조회")
    sep()

    tr_id = "VTTC8434R" if IS_MOCK else "TTTC8434R"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        APP_KEY,
        "appsecret":     APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }
    params = {
        "CANO":                  CANO,
        "ACNT_PRDT_CD":          ACNT_PRDT_CD,
        "AFHR_FLPR_YN":         "N",
        "OFL_YN":               "",
        "INQR_DVSN":            "02",
        "UNPR_DVSN":            "01",
        "FUND_STTL_ICLD_YN":    "N",
        "FNCG_AMT_AUTO_RDPT_YN":"N",
        "PRCS_DVSN":            "01",
        "CTX_AREA_FK100":       "",
        "CTX_AREA_NK100":       "",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers, params=params)

        data = res.json()

        if res.status_code == 200 and data.get("rt_cd") == "0":
            output2   = data.get("output2", [{}])
            summary   = output2[0] if output2 else {}
            cash      = int(summary.get("nxdy_excc_amt", 0))
            total     = int(summary.get("tot_evlu_amt", 0))
            holdings  = data.get("output1", [])
            filled    = [h for h in holdings if int(h.get("hldg_qty", 0)) > 0]

            ok(f"잔고 조회 성공")
            ok(f"예수금(익일): {cash:,}원")
            ok(f"총 평가금액: {total:,}원")
            ok(f"보유 종목 수: {len(filled)}개")
            for h in filled[:3]:
                ok(f"  └ {h.get('prdt_name','')} {int(h.get('hldg_qty',0))}주  "
                   f"평균 {float(h.get('pchs_avg_pric',0)):,.0f}원")
            return True
        else:
            fail(f"잔고 조회 실패 (HTTP {res.status_code})")
            msg = data.get("msg1", "")
            info(f"rt_cd: {data.get('rt_cd')}  msg: {msg}")
            if "계좌" in msg or "CANO" in msg:
                info(f"→ 계좌번호 형식을 확인하세요: KIS_ACCOUNT_NO={ACCOUNT_NO}")
                info(f"  예) 모의투자: 99501234-01  실전: 12345678-01")
            return False

    except Exception as e:
        fail(f"잔고 조회 오류: {e}")
        return False


# ── STEP 6: 소액 모의 매수 주문 ───────────────────────────────────────────────
async def step6_test_order(token: str):
    print("\n[STEP 6] 소액 모의 매수 주문 테스트")
    sep()

    if not IS_MOCK:
        info("실전 투자 모드 — 안전을 위해 주문 테스트를 건너뜁니다")
        info("KIS_MOCK=true 로 변경 후 모의투자에서 먼저 테스트하세요")
        return True

    # 삼성전자 1주 시장가 매수 (모의투자)
    tr_id = "VTTC0802U"
    url   = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey":        APP_KEY,
        "appsecret":     APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }
    body = {
        "CANO":         CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO":         "005930",
        "ORD_DVSN":     "01",      # 시장가
        "ORD_QTY":      "1",
        "ORD_UNPR":     "0",
    }

    info("삼성전자 1주 시장가 매수 요청 중...")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(url, headers=headers, json=body)

        data = res.json()
        rt_cd = data.get("rt_cd")

        if res.status_code == 200 and rt_cd == "0":
            output   = data.get("output", {})
            order_no = output.get("odno", "")
            ok(f"모의 매수 주문 성공!")
            ok(f"주문번호: {order_no}")
            ok(f"메시지: {data.get('msg1', '')}")
            info("→ 실제 체결 여부는 KIS 모의투자 HTS에서 확인하세요")
            return True
        else:
            fail(f"주문 실패 (HTTP {res.status_code}, rt_cd={rt_cd})")
            msg = data.get("msg1", "")
            info(f"메시지: {msg}")

            # 자주 발생하는 오류 안내
            if "잔고" in msg or "금액" in msg:
                info("→ 모의투자 예수금이 부족합니다. KIS HTS에서 모의투자 예수금을 충전하세요")
            elif "시간" in msg or "거래" in msg:
                info("→ 장 운영 시간(09:00~15:30) 외에는 모의 주문이 불가할 수 있습니다")
            elif "CANO" in msg or "계좌" in msg:
                info("→ 계좌번호가 모의투자 계좌번호인지 확인하세요")
            return False

    except Exception as e:
        fail(f"주문 요청 오류: {e}")
        return False


# ── 전체 실행 ──────────────────────────────────────────────────────────────────
async def main():
    print("=" * 55)
    print("  AI INVEST — KIS API 연동 검증 도구")
    print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 55)

    results = {}

    # STEP 1
    results["step1"] = step1_check_env()
    if not results["step1"]:
        print("\n⛔ STEP 1 실패 — .env 파일을 먼저 설정하고 다시 실행하세요")
        print_summary(results)
        return

    # STEP 2
    results["step2"] = await step2_check_network()
    if not results["step2"]:
        print("\n⛔ STEP 2 실패 — 네트워크/방화벽 문제를 해결하고 다시 실행하세요")
        print_summary(results)
        return

    # STEP 3
    token = await step3_get_token()
    results["step3"] = token is not None
    if not token:
        print("\n⛔ STEP 3 실패 — APP_KEY/APP_SECRET을 확인하고 다시 실행하세요")
        print_summary(results)
        return

    # STEP 4
    results["step4"] = await step4_get_price(token)

    # STEP 5
    results["step5"] = await step5_get_balance(token)

    # STEP 6
    results["step6"] = await step6_test_order(token)

    print_summary(results)


def print_summary(results: dict):
    print("\n" + "=" * 55)
    print("  검증 결과 요약")
    print("=" * 55)

    labels = {
        "step1": "환경변수 설정",
        "step2": "네트워크 연결",
        "step3": "토큰 발급",
        "step4": "현재가 조회",
        "step5": "잔고 조회",
        "step6": "모의 매수 주문",
    }

    all_pass = True
    for key, label in labels.items():
        status = results.get(key)
        if status is True:
            print(f"  ✅ {label}")
        elif status is False:
            print(f"  ❌ {label}")
            all_pass = False
        else:
            print(f"  ⏭️  {label} (건너뜀)")

    print("=" * 55)
    if all_pass:
        print("  🎉 모든 검증 통과! KIS API 연동 준비 완료")
        print("  다음 단계: 실전 투자 전 2주 모의투자 운영을 권장합니다")
    else:
        print("  ⚠️  일부 단계 실패 — 가이드를 참고해 설정을 수정하세요")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
