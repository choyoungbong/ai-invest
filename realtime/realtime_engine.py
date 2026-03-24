active_trades = {}

def add_trade(trade):
    active_trades[trade['code']] = {
        "buy_price": trade['price'],
        "stop_loss": trade['price'] * 0.98,
        "take_profit": trade['price'] * 1.05,
        "selling": False
    }

def remove_trade(code):
    if code in active_trades:
        del active_trades[code]


def handle_price_update(code, current_price):
    if code not in active_trades:
        return
    
    trade = active_trades[code]

    # 중복 매도 방지
    if trade["selling"]:
        return

    # 손절
    if current_price <= trade["stop_loss"]:
        trade["selling"] = True
        execute_sell(code, current_price, "STOP_LOSS")

    # 익절
    elif current_price >= trade["take_profit"]:
        trade["selling"] = True
        execute_sell(code, current_price, "TAKE_PROFIT")


def execute_sell(code, price, reason):
    print(f"[매도] {code} | {price} | {reason}")

    # TODO: 기존 주문 API 연결
    # place_sell_order(code, price)

    # TODO: DB 업데이트
    # update_trade_status(code, reason)

    remove_trade(code)
