# Asana 댓글 · High 우선순위 태스크 → 카카오 알림톡 (GitHub Actions 폴링, 서버 불필요)

자체 서버 없이, GitHub Actions의 무료 스케줄 실행 기능으로 5분마다 아사나 변경사항을
확인해서 카카오 알림톡을 보내는 구성입니다.

## 동작 방식

- `asana_poll_alimtalk.py`가 Asana Events API(sync 토큰 방식)로 "지난 실행 이후 생긴
  변경사항"만 가져옵니다.
- 댓글이 새로 달리면 댓글 알림 템플릿으로, 새 태스크가 우선순위 "High"로 등록되면
  High 우선순위 알림 템플릿으로 카카오 알림톡을 발송합니다.
- `.github/workflows/asana-alimtalk-poll.yml`이 5분마다(또는 Actions 탭에서 수동으로)
  이 스크립트를 실행하고, sync 토큰 상태를 `state/` 폴더에 커밋해서 다음 실행에 이어갑니다.

## 왜 public 저장소인가

GitHub Actions는 public 저장소에서 완전 무료·무제한이고, private 저장소는 월 2,000분
한도가 있습니다. 5분 간격(하루 288회)으로 계속 돌리면 private 저장소는 무료 한도를
금방 초과합니다. 그래서 이 저장소는 public으로 두는 것을 전제로 만들었고, 아사나
태스크명·댓글내용·담당자·수신자 전화번호 같은 내부 정보는 실행 로그에 찍히지 않도록
`asana_poll_alimtalk.py`가 기본적으로 마스킹되어 있습니다 (`LOG_SENSITIVE_DETAILS`를
`true`로 바꾸지 마세요). 그래도 아사나 토큰·솔라피 키 등 진짜 민감한 값은 코드가 아니라
**GitHub Secrets**에만 저장되므로 저장소가 공개돼도 노출되지 않습니다.

내부 정보 노출이 조금이라도 걱정되면 이 저장소만 private으로 만들고 폴링 주기를
15~30분 정도로 늘리세요 (무료 한도 안에서 운영 가능, 다만 실시간성은 떨어집니다).

## 설정 순서

1. **새 GitHub 저장소 만들기** (Public) — 이 폴더 전체를 그대로 올립니다.
2. **카카오 알림톡 템플릿 2종 등록·승인** (아직 없다면)
   - 댓글 알림: `#{태스크명}`, `#{작성자}`, `#{댓글내용}`, `#{asana_link}`
   - High 우선순위 태스크 알림: `#{태스크명}`, `#{담당자}`, `#{마감일}`, `#{asana_link}`
3. **저장소 Settings → Secrets and variables → Actions → New repository secret** 에 아래 값을 등록
   - `ASANA_TOKEN`, `ASANA_PROJECT_GID`
   - `SOLAPI_API_KEY`, `SOLAPI_API_SECRET`, `SOLAPI_PF_ID`
   - `SOLAPI_TEMPLATE_ID_COMMENT`, `SOLAPI_TEMPLATE_ID_HIGH_PRIORITY`
   - `SENDER_PHONE`, `RECIPIENT_PHONES` (콤마로 여러 명 구분)
   - (선택) 같은 화면의 **Variables** 탭에 `PRIORITY_FIELD_NAME`, `PRIORITY_HIGH_VALUE`를
     등록 — 생략하면 각각 기본값 `Priority`, `High`로 동작합니다.
4. **저장소 Settings → Actions → General → Workflow permissions** 를
   **"Read and write permissions"** 로 변경 (sync 토큰 상태 파일을 커밋하려면 필요합니다).
5. **Actions 탭 → "Asana 댓글 및 High 우선순위 태스크 알림톡 폴링" → Run workflow** 로
   1회 수동 실행해서 정상 동작 확인
   - 첫 실행은 sync 토큰만 새로 발급받고 이벤트는 처리하지 않는 게 정상입니다
     (그 시점부터 쌓이는 변경사항을 다음 실행부터 수집합니다).
   - 이후 워크플로우가 자동으로 5분마다 실행됩니다.

## 파일 구성

- `asana_poll_alimtalk.py` — 폴링 + 발송 본체
- `.github/workflows/asana-alimtalk-poll.yml` — 5분마다 실행하는 GitHub Actions 워크플로우
- `state/` — sync 토큰과 마지막 실행 시각을 저장하는 폴더 (워크플로우가 자동으로 커밋)
- `.env.example` — 로컬에서 직접 테스트하고 싶을 때 참고용 (`.env`로 복사해서 값 채우기,
  GitHub Actions 실행에는 사용되지 않음)

## 참고

- GitHub이 허용하는 스케줄 최소 간격은 5분이며, 부하가 높은 시간대에는 실제 실행이
  몇 분 지연될 수 있습니다(공식적으로 "best-effort" 방식).
- 60일간 저장소에 아무 활동이 없으면 GitHub이 예약 실행을 자동으로 중단시키는데,
  이 워크플로우는 매 실행마다 `state/` 커밋을 남기기 때문에 그 걱정은 하지 않아도 됩니다.
- 완전한 즉시 알림(초 단위)이 필요해지면, 별도로 준비된 상시 웹훅 서버 방식
  (`asana_webhook_server.py` + `register_asana_webhook.py`)으로 전환할 수 있습니다 —
  다만 그 방식은 공개적으로 접근 가능한 HTTPS 서버를 계속 띄워둬야 해서 GitHub Actions로는
  대체할 수 없습니다(Actions는 예약/수동 실행만 가능하고, 외부에서 오는 요청을 상시
  수신하는 용도로는 쓸 수 없습니다).
