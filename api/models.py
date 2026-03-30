"""
DB 모델 — 수수료/슬리피지/부분체결/기술지표 컬럼 포함
  + 분할매수: Trade.phase / Trade.parent_trade_id
  + 블랙리스트: StockBlacklist
"""
from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean, Text, BigInteger, Numeric
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()


def gen_uuid():
    return str(uuid.uuid4())


class Stock(Base):
    """종목 마스터 테이블"""
    __tablename__ = "stocks"
    code       = Column(String(10), primary_key=True)
    name       = Column(String(100), nullable=False)
    market     = Column(String(10))
    sector     = Column(String(100))
    listed_at  = Column(DateTime)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MarketData(Base):
    """실시간 시세 데이터"""
    __tablename__ = "market_data"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    code          = Column(String(10), nullable=False, index=True)
    open          = Column(Float)
    high          = Column(Float)
    low           = Column(Float)
    close         = Column(Float)
    volume        = Column(BigInteger)
    trading_value = Column(BigInteger)
    change_rate   = Column(Float)
    timestamp     = Column(DateTime, nullable=False, index=True)
    created_at    = Column(DateTime, server_default=func.now())


class ScanResult(Base):
    """스캐너 결과"""
    __tablename__ = "scan_results"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    scan_time     = Column(DateTime, nullable=False, index=True)
    code          = Column(String(10), nullable=False)
    name          = Column(String(100))
    rank          = Column(Integer)
    close         = Column(Float)
    volume        = Column(BigInteger)
    trading_value = Column(BigInteger)
    change_rate   = Column(Float)
    created_at    = Column(DateTime, server_default=func.now())


class Signal(Base):
    """전략 신호 — 기술지표 스냅샷 포함"""
    __tablename__ = "signals"
    id           = Column(String(36), primary_key=True, default=gen_uuid)
    code         = Column(String(10), nullable=False, index=True)
    name         = Column(String(100))
    signal_type  = Column(String(20), nullable=False)
    strategy     = Column(String(50), nullable=False)
    price        = Column(Float, nullable=False)
    target_price = Column(Float)
    stop_loss    = Column(Float)
    reason       = Column(Text)
    confidence   = Column(Float)
    # 기술지표 스냅샷
    rsi          = Column(Float)
    macd         = Column(Float)
    macd_signal  = Column(Float)
    bb_upper     = Column(Float)
    bb_lower     = Column(Float)
    atr          = Column(Float)
    vwap         = Column(Float)
    volume_spike = Column(Float)
    is_executed  = Column(Boolean, default=False)
    created_at   = Column(DateTime, server_default=func.now(), index=True)
    updated_at   = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Trade(Base):
    """
    체결 내역
    status: PENDING / PARTIAL / FILLED / CANCELLED / FAILED

    [분할매수 필드]
      phase           : 1=1차매수, 2=2차매수 (기본 1)
      parent_trade_id : 2차 매수 시 1차 매수 trade.id 참조
    """
    __tablename__ = "trades"
    id               = Column(String(36), primary_key=True, default=gen_uuid)
    signal_id        = Column(String(36), index=True)
    code             = Column(String(10), nullable=False)
    name             = Column(String(100))
    order_type       = Column(String(10))         # BUY / SELL
    order_price      = Column(Float)              # 주문 시 가격
    price            = Column(Float)              # 실제 체결 평균가
    order_quantity   = Column(Integer)            # 주문 수량
    quantity         = Column(Integer)            # 실제 체결 수량
    amount           = Column(Float)              # 체결금액
    # 수수료 & 슬리피지
    commission       = Column(Float, default=0)
    slippage         = Column(Float, default=0)
    theory_profit    = Column(Float)              # 이론 수익
    real_profit      = Column(Float)              # 실전 수익 (수수료/슬리피지 반영)
    # 분할매수
    phase            = Column(Integer, default=1) # 1=1차, 2=2차
    parent_trade_id  = Column(String(36), nullable=True)  # 2차 매수 시 1차 trade ID
    # 상태
    status           = Column(String(20), default="PENDING")
    broker_order_id  = Column(String(100))
    is_simulation    = Column(Boolean, default=True)
    created_at       = Column(DateTime, server_default=func.now())
    filled_at        = Column(DateTime)


class DailyStats(Base):
    """일별 손익 통계"""
    __tablename__ = "daily_stats"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    date           = Column(String(10), nullable=False, unique=True, index=True)
    total_trades   = Column(Integer, default=0)
    win_count      = Column(Integer, default=0)
    lose_count     = Column(Integer, default=0)
    gross_profit   = Column(Float, default=0)
    commission_sum = Column(Float, default=0)
    slippage_sum   = Column(Float, default=0)
    net_profit     = Column(Float, default=0)
    created_at     = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockBlacklist(Base):
    """
    손절 후 재진입 금지 블랙리스트
    expires_at 이후 자동 해제
    """
    __tablename__ = "stock_blacklist"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    code           = Column(String(10), nullable=False, index=True)
    name           = Column(String(100))
    reason         = Column(String(200))
    blacklisted_at = Column(DateTime, server_default=func.now())
    expires_at     = Column(DateTime, nullable=False, index=True)
