import websocket
import json
from realtime_engine import handle_price_update

def on_message(ws, message):
    data = json.loads(message)

    # ⚠️ 여기 KIS 데이터 구조에 맞게 수정 필요
    code = data.get("code")
    price = data.get("price")

    if code and price:
        handle_price_update(code, float(price))


def on_open(ws):
    print("WebSocket 연결됨")

    # TODO: 구독 요청
    subscribe_msg = {
        "type": "subscribe",
        "codes": ["005930"]  # 테스트용
    }

    ws.send(json.dumps(subscribe_msg))


def start_ws():
    ws = websocket.WebSocketApp(
        "wss://api.kis.com/real",
        on_message=on_message,
        on_open=on_open
    )

    ws.run_forever()
