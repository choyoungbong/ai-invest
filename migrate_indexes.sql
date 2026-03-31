-- ============================================================
-- AI-INVEST DB 인덱스 추가 마이그레이션 (성능 개선)
-- 적용: 장 마감 후 실행 (잠금 없는 CONCURRENTLY 옵션 사용)
-- 명령: docker exec -it aiinvest-postgres psql -U aiinvest -d aiinvest -f /tmp/migrate_indexes.sql
-- ============================================================

-- CONCURRENTLY: 실행 중에도 테이블 잠금 없이 인덱스 생성 (운영 중 적용 가능)
-- 단, CONCURRENTLY는 트랜잭션 내에서 실행 불가 → BEGIN/COMMIT 없이 실행

-- 1. 분할매수 조회 최적화 (signal_id + phase 복합)
--    check_and_execute_phase2() 에서 phase=1 조건 조회 시 사용
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trade_signal_phase
    ON trades(signal_id, phase);

-- 2. 종목별 상태 조회 최적화 (code + status 복합)
--    _has_open_position(), check_overtrading() 에서 자주 사용
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trade_code_status
    ON trades(code, status);

-- 3. 청산 체크 최적화 (signal_id + order_type + status 복합)
--    _get_open_position() SELL 체크에서 자주 사용
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trade_signal_order_status
    ON trades(signal_id, order_type, status);

-- 4. 블랙리스트 만료 조회 최적화 (이미 expires_at 단일 인덱스 있음, 복합 추가)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_blacklist_code_expires
    ON stock_blacklist(code, expires_at);

-- 확인 쿼리
SELECT
    indexname,
    tablename,
    indexdef
FROM pg_indexes
WHERE tablename IN ('trades', 'stock_blacklist')
  AND indexname LIKE 'idx_%'
ORDER BY tablename, indexname;

-- ============================================================
-- 롤백:
-- DROP INDEX CONCURRENTLY IF EXISTS idx_trade_signal_phase;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_trade_code_status;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_trade_signal_order_status;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_blacklist_code_expires;
-- ============================================================
