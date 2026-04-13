수정 내용
- build_multi_update_payload(): items 와 multiProductUpdateRequestVos 를 동시에 전송하도록 변경
- push_stock_updates(): 두 필드 중 하나라도 있으면 전송하도록 변경

적용 방법
1. 기존 app.py를 이 ZIP 안의 app.py로 교체
2. GitHub/Render 등에 다시 배포
3. 서비스 재시작 후 자동 실행 다시 테스트

주의
- 이전 배포본이 그대로 살아 있으면 같은 오류가 계속 날 수 있습니다.
- 반드시 새 app.py 업로드 후 재배포/재시작까지 해주세요.
