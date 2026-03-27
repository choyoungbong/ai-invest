"""
Database — DB 커넥션 풀 안정화 (Phase 9)
SQLAlchemy AsyncSession + Singleton 패턴

개선사항:
- 커넥션 풀 파라미터 최적화
- pre_ping으로 끊긴 연결 자동 감지/복구
- Railway/Cloud 환경 URL 자동 변환
- 재연결 시도 로직
"""
import logging
import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool

from .models import Base

logger = logging.getLogger(__name__)

# ── URL 자동 변환 ─────────────────────────────────────────────────────────────
_raw_url = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://aiinvest:aiinvest@postgres:5432/aiinvest"
)

if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _raw_url.startswith("postgresql://"):
    _raw_url = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

DATABASE_URL = _raw_url

# ── 환경 감지 ─────────────────────────────────────────────────────────────────
IS_SERVERLESS = bool(
    os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("VERCEL")
    or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
)

# ── 엔진 생성 ─────────────────────────────────────────────────────────────────
_engine_kwargs: dict = {
    "echo":          False,
    "pool_pre_ping": True,   # 끊긴 연결 자동 감지
}

if IS_SERVERLESS:
    _engine_kwargs["poolclass"] = NullPool
    logger.info("DB: Serverless 모드 — NullPool 사용")
else:
    _engine_kwargs.update({
        "pool_size":         5,
        "max_overflow":      10,
        "pool_timeout":      30,
        "pool_recycle":      1800,   # 30분마다 연결 재생성
        "connect_args": {
            "command_timeout":        10,
            "server_settings": {
                "application_name": "ai-invest",
            },
        },
    })
    logger.info("DB: 일반 서버 모드 — AsyncAdaptedQueuePool 사용")

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ── DB 초기화 ─────────────────────────────────────────────────────────────────

async def init_db():
    """테이블 자동 생성"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB 초기화 완료")


# ── 세션 팩토리 ───────────────────────────────────────────────────────────────

async def get_db() -> AsyncSession:
    """FastAPI Depends용 세션 생성기"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── 헬스체크 ──────────────────────────────────────────────────────────────────

async def check_db_health() -> bool:
    """DB 연결 상태 확인"""
    try:
        import sqlalchemy
        async with AsyncSessionLocal() as session:
            await session.execute(sqlalchemy.text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"DB 헬스체크 실패: {e}")
        return False
