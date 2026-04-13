# 자동모드 설정 순서 (맥미니 중계서버 연동)

1. 이 폴더 전체를 GitHub 저장소에 업로드합니다.
2. Render 환경변수에 아래 값을 넣습니다.
   - RELAY_BASE_URL=https://relay.내도메인.com
   - RELAY_SHARED_TOKEN=맥미니 `~/naver-relay/.env` 의 `RELAY_SHARED_TOKEN` 과 동일한 값
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
   - TELEGRAM_AUTO_POLL_SECONDS=5
3. 필요하면 `NAVER_PRODUCT_SEARCH_BODY` 환경변수에 네이버 상품 조회용 JSON을 넣습니다.
   - 기본값은 `{"page":1,"size":500}` 입니다.
4. 네이버 커머스API의 `API호출 IP` 는 Render가 아니라 **맥미니가 연결된 인터넷의 공인 IPv4** 로 등록해야 합니다.
5. 맥미니에서는 아래 두 프로세스가 모두 켜져 있어야 합니다.
   - `uvicorn relay_app:app --host 0.0.0.0 --port 8000`
   - `cloudflared tunnel run naver-relay`
6. 배포 후 `재고일괄변경` 으로 들어갑니다.
7. 입력 방식에서 `자동`을 고릅니다.
8. `자동 실행 시작` 을 누릅니다.
9. 텔레그램으로 온 메시지에 **한 번만** 답장합니다.
   - 예시
     ```
     1 5
     2 0
     3 10
     ```
10. 앱이 첫 번째 유효한 답장 1개를 자동 처리합니다.
11. 메모 텍스트를 다시 텔레그램으로 보냅니다. 메모 텍스트에는 0도 포함됩니다.
12. 실제 네이버 반영은 0보다 큰 값만 중계서버를 통해 수행합니다.

주의:
- 자동모드는 첫 번째 유효 답장 1개만 처리합니다.
- 답장은 반드시 한 메시지로 보내세요.
- 형식은 `번호 수량` 입니다.
- 수동모드는 기존과 동일합니다.
- Render는 네이버를 직접 호출하지 않고 `RELAY_BASE_URL` 로 중계서버를 호출합니다.
