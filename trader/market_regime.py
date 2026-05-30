"""
시장 레짐 필터 — Market Breadth 기반

전체 종목 중 20일 이평선 위에 있는 비율로 시장 상태 판단:
  bull    (≥ BULL_THR): 매수 허용
  bear    (≤ BEAR_THR): 신규 매수 차단
  neutral (그 사이)   : 매수 허용

환경변수:
  REGIME_FILTER_ENABLED : true/false (기본 true)
  REGIME_BULL_THRESHOLD : bull 기준 하한 (기본 0.45)
  REGIME_BEAR_THRESHOLD : bear 기준 상한 (기본 0.35)
"""
import logging, os
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)

REGIME_FILTER_ENABLED = os.getenv("REGIME_FILTER_ENABLED", "true").lower() == "true"
REGIME_BULL_THRESHOLD = float(os.getenv("REGIME_BULL_THRESHOLD", "0.45"))
REGIME_BEAR_THRESHOLD = float(os.getenv("REGIME_BEAR_THRESHOLD", "0.35"))

async def get_market_regime(db: AsyncSession) -> tuple[str, float]:
    """
    시장 레짐 반환: ('bull'|'neutral'|'bear', breadth_ratio)
    """
    if not REGIME_FILTER_ENABLED:
        return "bull", 1.0
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=40)).replace(tzinfo=None)
        result = await db.execute(text("""
            WITH ranked AS (
                SELECT code, close,
                       AVG(close) OVER (
                           PARTITION BY code
                           ORDER BY timestamp
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS ma20,
                       COUNT(*) OVER (
                           PARTITION BY code
                           ORDER BY timestamp
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS cnt,
                       ROW_NUMBER() OVER (
                           PARTITION BY code ORDER BY timestamp DESC
                       ) AS rn
                FROM market_data
                WHERE timestamp >= :cutoff AND volume > 0
            )
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN close > ma20 THEN 1 ELSE 0 END)    AS above,
                ROUND(AVG(close / NULLIF(ma20,0) - 1)::numeric * 100, 2) AS avg_gap_pct
            FROM ranked
            WHERE rn = 1 AND cnt >= 15
        """), {"cutoff": cutoff})

        row = result.fetchone()
        if not row or row.total < 50:
            logger.info(f"[레짐] 데이터 부족 ({row.total if row else 0}종목) → bull")
            return "bull", 1.0

        breadth = row.above / row.total

        if breadth >= REGIME_BULL_THRESHOLD:
            regime = "bull"
        elif breadth <= REGIME_BEAR_THRESHOLD:
            regime = "bear"
        else:
            regime = "neutral"

        logger.info(
            f"[레짐] {row.above}/{row.total} ({breadth:.1%}) "
            f"평균갭 {row.avg_gap_pct:+.2f}% → {regime.upper()}"
        )
        return regime, breadth

    except Exception as e:
        logger.warning(f"[레짐] 판단 실패: {e} → bull")
        return "bull", 1.0
