-- ============================================================
-- AI-INVEST 분할매수 DB 마이그레이션
-- 적용: 실전 전환 전, 장 마감 후 실행
-- 명령: docker exec -it aiinvest-postgres psql -U aiinvest -d aiinvest -f /tmp/migrate_split_buy.sql
-- ============================================================

BEGIN;

-- 1. trades 테이블: 분할매수 컬럼 추가
ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS phase            INTEGER  DEFAULT 1,
  ADD COLUMN IF NOT EXISTS parent_trade_id VARCHAR(36);

-- 기존 데이터는 모두 1차 매수로 처리
UPDATE trades SET phase = 1 WHERE phase IS NULL;

COMMENT ON COLUMN trades.phase           IS '분할매수 차수: 1=1차매수, 2=2차매수';
COMMENT ON COLUMN trades.parent_trade_id IS '2차 매수 시 1차 매수 trade.id 참조';

-- 2. stock_blacklist 테이블 생성 (손절 후 재진입 금지)
CREATE TABLE IF NOT EXISTS stock_blacklist (
    id             SERIAL PRIMARY KEY,
    code           VARCHAR(10)  NOT NULL,
    name           VARCHAR(100),
    reason         VARCHAR(200),
    blacklisted_at TIMESTAMP    DEFAULT NOW(),
    expires_at     TIMESTAMP    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blacklist_code       ON stock_blacklist (code);
CREATE INDEX IF NOT EXISTS idx_blacklist_expires_at ON stock_blacklist (expires_at);

COMMENT ON TABLE stock_blacklist IS '손절 청산 후 재진입 금지 블랙리스트';

-- 3. 확인 쿼리
SELECT
  (SELECT COUNT(*) FROM trades WHERE phase = 2) AS "2차매수건수",
  (SELECT COUNT(*) FROM stock_blacklist)         AS "블랙리스트건수";

COMMIT;

-- ============================================================
-- 롤백이 필요할 경우:
-- ALTER TABLE trades DROP COLUMN IF EXISTS phase;
-- ALTER TABLE trades DROP COLUMN IF EXISTS parent_trade_id;
-- DROP TABLE IF EXISTS stock_blacklist;
-- ============================================================
