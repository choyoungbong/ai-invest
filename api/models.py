from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean, Text, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()


def gen_uuid():
    return str(uuid.uuid4())


class Stock(Base):
    """종목 마스터 테이블"""
    __tablename__ = "stocks"

    code = Column(String(10), primary_key=True)
    name = Column(String(100), nullable=False)
    market = Column(String(10))  # KOSPI / KOSDAQ
    sector = Column(String(100))
    listed_at = Column(DateTime)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MarketData(Base):
    """실시간 시세 데이터"""
    __tablename__ = "market_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(BigInteger)
    trading_value = Column(BigInteger)   # 거래대금
    change_rate = Column(Float)          # 등락률 %
    timestamp = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())


class ScanResult(Base):
    """스캐너 결과 – 거래대금 상위"""
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_time = Column(DateTime, nullable=False, index=True)
    code = Column(String(10), nullable=False)
    name = Column(String(100))
    rank = Column(Integer)
    close = Column(Float)
    volume = Column(BigInteger)
    trading_value = Column(BigInteger)
    change_rate = Column(Float)
    created_at = Column(DateTime, server_default=func.now())


class Signal(Base):
    """전략 신호"""
    __tablename__ = "signals"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(100))
    signal_type = Column(String(20), nullable=False)   # BUY / SELL
    strategy = Column(String(50), nullable=False)       # breakout / etc
    price = Column(Float, nullable=False)
    target_price = Column(Float)
    stop_loss = Column(Float)
    reason = Column(Text)
    confidence = Column(Float)                          # 0.0 ~ 1.0
    is_executed = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Trade(Base):
    """체결 내역"""
    __tablename__ = "trades"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    signal_id = Column(String(36), index=True)
    code = Column(String(10), nullable=False)
    name = Column(String(100))
    order_type = Column(String(10))   # BUY / SELL
    price = Column(Float)
    quantity = Column(Integer)
    amount = Column(Float)            # price * quantity
    status = Column(String(20))       # PENDING / FILLED / CANCELLED
    broker_order_id = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())
    filled_at = Column(DateTime)
