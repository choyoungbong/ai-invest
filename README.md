# AI INVEST

AI 기반 한국 주식 자동매매 시스템

## 구성

```
ai-invest/
├── api/            # FastAPI 앱 + DB 모델
│   ├── main.py     # 라우터 통합
│   ├── models.py   # SQLAlchemy 모델
│   └── database.py # DB 연결
├── collector/      # pykrx 시세 수집
├── scanner/        # 거래대금 상위 스캐너
├── strategy/       # 돌파매매 전략
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## 빠른 시작

### 1. 환경 설정

```bash
cp .env.example .env
```

### 2. Docker 실행

```bash
docker compose up -d
```

서버: http://localhost:8000  
API 문서: http://localhost:8000/docs

### 3. 종목 마스터 동기화

```bash
curl -X POST http://localhost:8000/collector/sync-master
```

### 4. 시세 수집

```bash
curl -X POST http://localhost:8000/collector/collect
```

### 5. 스캐너 실행 (거래대금 상위 30)

```bash
curl http://localhost:8000/scanner/top-volume?top_n=30
```

### 6. 돌파매매 전략 실행

```bash
curl -X POST http://localhost:8000/strategy/run
```

### 7. 신호 조회

```bash
curl http://localhost:8000/signals
```

### 8. 주문 생성 (시뮬레이션)

```bash
curl -X POST "http://localhost:8000/trade/order?signal_id=<ID>&quantity=10"
```

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | /health | 헬스체크 |
| POST | /collector/sync-master | 종목 마스터 동기화 |
| POST | /collector/collect | 당일 시세 수집 |
| GET | /scanner/top-volume | 거래대금 상위 조회 |
| POST | /scanner/run | 스캐너 실행+저장 |
| POST | /strategy/run | 스캔→돌파전략 일괄실행 |
| GET | /signals | 신호 목록 |
| GET | /signals/{id} | 신호 상세 |
| POST | /trade/order | 주문 생성 |
| GET | /trades | 체결 내역 |

---

## 돌파매매 전략 조건

- 당일 고가 > 최근 20일 최고가
- 거래대금 ≥ 20일 평균 × 2배
- 등락률 ≥ 2%
- 손절: -2%
- 목표수익: +4%

---

## 개발 로드맵

- [x] Phase 1 – Collector (pykrx 시세 수집)
- [x] Phase 2 – Scanner (거래대금 상위)
- [x] Phase 3 – Strategy (돌파매매)
- [ ] Phase 4 – Notification (텔레그램)
- [ ] Phase 5 – Trader (브로커 API 연동)
- [ ] Phase 6 – AI Analysis (신호 설명 AI)
- [ ] Phase 7 – Dashboard (Next.js)
