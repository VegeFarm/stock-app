매출계산 Google Drive 날짜 폴더 자동 계산 기능 추가본

수정 파일:
1) stock-app-main/app.py
2) stock-app-main/render_env_example.txt

추가된 기능:
- 매출계산 메뉴에서 날짜를 선택하지 않으면 오늘 날짜가 기본값입니다.
- 선택 날짜 기준으로 Google Drive의 판매내역/월.일 폴더를 찾습니다. 예: 4월 25일 -> 판매내역/4.25
- 해당 폴더 안의 .xlsx 파일을 전부 다운로드해 기존 매출계산 로직으로 계산합니다.
- 총 주문금액은 지정 Google Sheet의 해당월 시트 B열에 기록합니다.
- 인원×3,500원 결과는 C열에 기록합니다.
- A열에 해당 날짜가 없으면 새 행을 만들어 A열에 M/D 형식으로 기록합니다. 예: 4/25
- 해당월 시트(예: 4월)가 없으면 새 시트를 만들지 않고 기록을 건너뜁니다.

필요 환경변수:
- GOOGLE_SERVICE_ACCOUNT_JSON: 기존 Google Drive 저장 기능과 동일하게 필요합니다.
- GOOGLE_DRIVE_SALES_ROOT_FOLDER_NAME: 기본값 판매내역
- GOOGLE_DRIVE_SALES_ROOT_FOLDER_ID: 선택, 지정하면 같은 이름 폴더 혼동 방지
- SALES_RESULT_SPREADSHEET_ID: 선택, 앱 화면에서 직접 Google Sheet ID/URL을 입력해도 됩니다.

권한:
- 서비스 계정 이메일에 판매내역 Drive 폴더 접근 권한이 있어야 합니다.
- 서비스 계정 이메일에 결과 기록용 Google Sheet 편집 권한이 있어야 합니다.
