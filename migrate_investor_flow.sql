-- ──────────────────────────────────────────────────────────────────────────────
-- migrate_investor_flow.sql
-- 외국인/기관 수급 데이터 컬럼 추가
--
-- 적용 방법:
--   docker exec -i aiinvest-postgres psql -U aiinvest -d aiinvest < migrate_investor_flow.sql
--
-- 특이사항:
--   - IF NOT EXISTS 사용 → 이미 적용된 환경에서 재실행해도 오류 없음
--   - 모두 NULL 허용 → 기존 데이터 및 기존 코드에 영향 없음
-- ──────────────────────────────────────────────────────────────────────────────

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS foreign_net_buy     INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS institution_net_buy  INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS investor_score       FLOAT   DEFAULT NULL;

-- 적용 확인
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'signals'
  AND column_name IN ('foreign_net_buy', 'institution_net_buy', 'investor_score')
ORDER BY column_name;
