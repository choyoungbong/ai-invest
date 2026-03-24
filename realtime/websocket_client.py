import websocket
import json
from realtime.realtime_engine import handle_price_update

# 테스트용 (바이낸스 실시간 BTC 가격)
WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"


def on_message(ws, message):
    data = json.loads(message)

    # 바이낸스 데이터 구조
    price = float(data['p'])   # 체결가

    # 테스트용 코드
    code = "TEST"

    print(f"[실시간] {code} 가격: {price}")

    handle_price_update(code, price)


def on_open(ws):
    print("WebSocket 연결됨 (테스트)")


def on_error(ws, error):
    print("WebSocket 오류:", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket 종료됨")


def start_ws():
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever()
