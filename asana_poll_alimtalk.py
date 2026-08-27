"""
Asana 프로젝트 짧은 주기 폴링 → 신규 댓글 / High 우선순위 태스크 등록 시 카카오 알림톡 발송
(cron 등 외부 스케줄러로 반복 실행하는 방식 — 상시 서버 불필요)

플로우
  1) 이 스크립트는 실행될 때마다 Asana Events API(GET /events, sync 토큰 방식)로
     지난 실행 이후 쌓인 변경사항만 가져옵니다. (webhook과 내부적으로 같은
     이벤트 버스를 쓰기 때문에, 단순히 tasks를 modified_since로 훑는 것보다
     신뢰도가 높습니다.)
  2) 이벤트 중 "댓글(스토리) 추가"는 즉시 댓글 알림 템플릿으로 발송합니다.
  3) "태스크 추가" 이벤트는 태스크를 다시 조회해서 생성 시점에 이미 우선순위가
     "High"로 지정돼 있는 경우에만 High 우선순위 알림 템플릿으로 발송합니다.
  4) "기존 태스크의 우선순위를 나중에 High로 바꾼 경우"는 3)의 task 이벤트로는
     잡히지 않습니다 — 아사나가 2026-08-02 무렵부터 task의 "changed" 이벤트
     (커스텀필드 변경 포함)를 웹훅/Events API 양쪽에서 안정적으로 보내주지 않는
     회귀가 있기 때문입니다(아사나 개발자 포럼에 다수 보고, 공식 수정 없음).
     대신 커스텀필드 값이 바뀔 때 자동으로 남는 스토리(resource_subtype=
     enum_custom_field_changed — "story added" 이벤트라 정상적으로 전달됨)를
     감지해서, 그 변경이 우리가 감시하는 우선순위 필드이고 새 값이 "High"일
     때만 발송합니다.
  5) crontab 등으로 이 스크립트를 1~5분 간격으로 반복 실행하면 되고, 상시 켜져
     있는 서버는 필요 없습니다. 다만 완전한 즉시성은 아니고 "몇 분 이내"의
     준실시간(near real-time)입니다 — Asana 문서 기준 이벤트 반영 지연은 평균
     1분, 최대 10분까지 걸릴 수 있어 폴링 주기와 합쳐지면 최악의 경우 10여 분
     늦어질 수 있습니다. 완전한 즉시 알림이 필요하면 상시 웹훅 서버 방식
     (asana_webhook_server.py — 다만 이 스크립트도 4)의 회귀 이슈를 반영하려면
     동일하게 업데이트가 필요합니다)을 쓰세요.

사전 준비 (기존 asana_weekly_summary_alimtalk_multi.py와 동일 + 아래 추가)
  - 아래 두 알림톡 템플릿을 각각 카카오에 등록·승인받아야 합니다.
      [댓글 알림 템플릿] 변수: #{태스크명}, #{작성자}, #{댓글내용}, #{asana_link}
      [High 우선순위 태스크 알림 템플릿] 변수: #{태스크명}, #{담당자}, #{마감일}, #{asana_link}
  - Asana 프로젝트에 우선순위를 나타내는 단일선택 커스텀 필드가 있어야 하며
    (기본값은 "Priority", 옵션 중 하나가 "High"), 다르면 PRIORITY_FIELD_NAME /
    PRIORITY_HIGH_VALUE 환경변수로 맞춰주세요.
  - 이 스크립트가 sync 토큰을 저장할 파일(SYNC_TOKEN_FILE)에 쓸 수 있는 위치에서
    실행해야 하고, cron으로 반복 실행할 때도 항상 같은 경로를 써야 합니다
    (파일이 지워지면 그 시점 이전 변경사항은 다시 받을 수 없습니다 — 다만 토큰이
    만료된 경우에도 다음 실행부터는 정상적으로 다시 수집됩니다).
  - 자체 서버 없이 돌리려면 crontab 대신 GitHub Actions의 schedule 트리거로도
    실행할 수 있습니다 (별도로 드린 .github/workflows/asana-alimtalk-poll.yml,
    README.md 참고). 이 경우 SYNC_TOKEN_FILE 대신 워크플로우가 상태 파일을
    저장소에 커밋하는 방식으로 영속화합니다.
  - crontab으로 직접 운영한다면 설정 예시 (3분마다 실행):
      */3 * * * * cd /path/to/script && /usr/bin/python3 asana_poll_alimtalk.py >> poll.log 2>&1
  - 모든 키/토큰은 반드시 환경변수(로컬은 .env, GitHub Actions는 Secrets)로만
    주입하세요 (코드에 하드코딩 금지).
  - LOG_SENSITIVE_DETAILS=true로 설정하지 않는 한, 태스크명·댓글내용·담당자·
    전화번호 등 내부 정보는 콘솔 로그에 찍히지 않습니다 (public 저장소의 GitHub
    Actions 로그처럼 외부에 노출될 수 있는 환경을 기본값으로 가정).

필요 패키지
  pip install requests solapi python-dotenv
"""

import os
import json
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # python-dotenv가 없으면 시스템 환경변수만 사용

# ------------------------------------------------------------------
# 0. 환경설정
# ------------------------------------------------------------------
REQUIRED_ENV_VARS = [
    "ASANA_TOKEN",
    "ASANA_PROJECT_GID",
    "SOLAPI_API_KEY",
    "SOLAPI_API_SECRET",
    "SOLAPI_PF_ID",
    "SOLAPI_TEMPLATE_ID_COMMENT",
    "SOLAPI_TEMPLATE_ID_HIGH_PRIORITY",
    "SENDER_PHONE",
    "RECIPIENT_PHONES",
]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"환경변수 {name}가 설정되지 않았습니다. .env 또는 시크릿 매니저를 확인하세요.")
    return value


ASANA_TOKEN = os.environ.get("ASANA_TOKEN", "")
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")

SOLAPI_API_KEY = os.environ.get("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET", "")
SOLAPI_PF_ID = os.environ.get("SOLAPI_PF_ID", "")
SOLAPI_TEMPLATE_ID_COMMENT = os.environ.get("SOLAPI_TEMPLATE_ID_COMMENT", "")
SOLAPI_TEMPLATE_ID_HIGH_PRIORITY = os.environ.get("SOLAPI_TEMPLATE_ID_HIGH_PRIORITY", "")

SENDER_PHONE = os.environ.get("SENDER_PHONE", "")
# 여러 명에게 보내려면 콤마(,)로 구분: "01011112222,01033334444"
RECIPIENT_PHONES = [
    p.strip() for p in os.environ.get("RECIPIENT_PHONES", "").split(",") if p.strip()
]

# 우선순위 커스텀 필드명 / "High"에 해당하는 값 (프로젝트마다 다르면 .env에서 재정의)
PRIORITY_FIELD_NAME = os.environ.get("PRIORITY_FIELD_NAME", "Priority")
PRIORITY_HIGH_VALUE = os.environ.get("PRIORITY_HIGH_VALUE", "High")

# GitHub Actions(특히 public 저장소)처럼 실행 로그가 외부에 노출될 수 있는 환경에서는
# 기본값(false)을 유지해서 태스크명·댓글내용·담당자·전화번호 같은 내부 정보가
# 로그에 그대로 찍히지 않도록 합니다. 로컬 디버깅 시에만 "true"로 설정하세요.
LOG_SENSITIVE_DETAILS = os.environ.get("LOG_SENSITIVE_DETAILS", "false").lower() == "true"


def _mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"

# 마지막으로 처리한 지점을 가리키는 sync 토큰 저장 위치.
# 실행할 때마다 이 파일을 읽고, 처리에 성공하면 새 토큰으로 덮어씁니다.
SYNC_TOKEN_FILE = Path(
    os.environ.get("SYNC_TOKEN_FILE", str(Path(__file__).resolve().parent / ".asana_sync_token"))
)

ASANA_API_BASE = "https://app.asana.com/api/1.0"
ASANA_HEADERS = {"Authorization": f"Bearer {ASANA_TOKEN}"}


# ------------------------------------------------------------------
# 1. sync 토큰 저장/조회
# ------------------------------------------------------------------
def _load_sync_token() -> str:
    if SYNC_TOKEN_FILE.exists():
        return SYNC_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def _save_sync_token(token: str) -> None:
    SYNC_TOKEN_FILE.write_text(token, encoding="utf-8")


# ------------------------------------------------------------------
# 2. Asana Events API 폴링
# ------------------------------------------------------------------
def fetch_events() -> list:
    """지난 실행 이후 쌓인 이벤트를 모두 가져옵니다 (has_more 페이지네이션 포함).

    첫 실행(토큰 없음)이거나 토큰이 만료된 경우 412가 올 수 있는데, 이때는
    Asana가 응답에 새 토큰을 함께 내려주므로 그 토큰만 저장하고 이번 실행에서는
    이벤트 없이 종료합니다 (다음 실행부터 정상적으로 변경사항을 수집합니다).
    """
    sync_token = _load_sync_token()
    all_events = []

    while True:
        params = {"resource": ASANA_PROJECT_GID}
        if sync_token:
            params["sync"] = sync_token

        resp = requests.get(f"{ASANA_API_BASE}/events", headers=ASANA_HEADERS, params=params, timeout=15)

        if resp.status_code == 412:
            new_token = resp.json().get("sync", "")
            if new_token:
                _save_sync_token(new_token)
                print("[안내] sync 토큰을 새로 발급받았습니다. 다음 실행부터 변경사항을 수집합니다.")
            return []

        resp.raise_for_status()
        body = resp.json()
        all_events.extend(body.get("data", []))
        sync_token = body.get("sync", sync_token)

        if not body.get("has_more"):
            _save_sync_token(sync_token)
            break

    return all_events


# ------------------------------------------------------------------
# 3. Asana API 조회 헬퍼
# ------------------------------------------------------------------
def fetch_story(story_gid: str) -> dict:
    """스토리(댓글 또는 커스텀필드 변경 로그 등) 상세 조회.

    target(부모 태스크) 이름/링크와, 커스텀필드 변경 스토리(resource_subtype=
    enum_custom_field_changed)를 판별하기 위한 custom_field.name / new_enum_value.name도
    함께 받아옵니다. 댓글이 아닌 스토리에는 이 필드들이 비어있을 뿐이라 문제없습니다.
    """
    url = f"{ASANA_API_BASE}/stories/{story_gid}"
    params = {
        "opt_fields": (
            "text,resource_subtype,created_by.name,created_at,"
            "target.name,target.permalink_url,target.gid,"
            "custom_field.name,new_enum_value.name"
        )
    }
    resp = requests.get(url, headers=ASANA_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def fetch_task(task_gid: str) -> dict:
    """태스크 상세 조회 (커스텀 필드 포함) — 이벤트 payload엔 없는 정보라 별도 호출 필요."""
    url = f"{ASANA_API_BASE}/tasks/{task_gid}"
    params = {
        "opt_fields": (
            "name,due_on,assignee.name,permalink_url,completed,"
            "custom_fields.name,custom_fields.display_value"
        )
    }
    resp = requests.get(url, headers=ASANA_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def get_custom_field_value(task: dict, field_name: str) -> str:
    for cf in task.get("custom_fields") or []:
        if cf.get("name") == field_name:
            return cf.get("display_value") or ""
    return ""


def _strip_scheme(url: str) -> str:
    """알림톡 버튼 URL 변수는 템플릿에 https://가 고정으로 박혀 있으므로 스킴을 뺀다."""
    return url.replace("https://", "").replace("http://", "")


# ------------------------------------------------------------------
# 4. 카카오 알림톡 발송 (Solapi) — 기존 스크립트와 동일한 방식
# ------------------------------------------------------------------
def send_alimtalk(template_id: str, variables: dict, phone: str) -> dict:
    from solapi import SolapiMessageService  # pip install solapi
    from solapi.model import RequestMessage

    message_service = SolapiMessageService(api_key=SOLAPI_API_KEY, api_secret=SOLAPI_API_SECRET)
    kakao_variables = {f"#{{{key}}}": value for key, value in variables.items()}

    message = RequestMessage(
        from_=SENDER_PHONE,
        to=phone,
        kakao_options={
            "pfId": SOLAPI_PF_ID,
            "templateId": template_id,
            "variables": kakao_variables,
        },
    )
    return message_service.send(message)


def send_to_all(template_id: str, variables: dict) -> None:
    for phone in RECIPIENT_PHONES:
        try:
            send_alimtalk(template_id, variables, phone)
            print(f"[성공] {_mask_phone(phone)} 발송 완료 (template={template_id})")
        except Exception as e:
            print(f"[실패] {_mask_phone(phone)} 발송 중 오류: {e}")


# ------------------------------------------------------------------
# 5. 이벤트별 처리
# ------------------------------------------------------------------
def handle_story_event(event: dict) -> None:
    """story(added) 이벤트 하나를 받아 종류에 따라 분기합니다.

    아사나가 2026-08-02 무렵부터 task의 "changed" 이벤트(커스텀필드 변경 포함)를
    웹훅/Events API 양쪽에서 안정적으로 보내주지 않는 회귀가 있어(아사나 개발자
    포럼에 다수 보고, 공식 수정 없음), "이미 있던 태스크의 Priority를 나중에
    High로 바꾼 경우"는 task 이벤트만으로는 감지할 수 없습니다. 대신 커스텀필드
    값이 바뀔 때 태스크에 자동으로 남는 스토리(resource_subtype=
    enum_custom_field_changed)를 이용합니다 — 이 스토리는 댓글과 마찬가지로
    "story added" 이벤트라 정상적으로 전달됩니다.
    """
    story_gid = event.get("resource", {}).get("gid")
    if not story_gid:
        return

    story = fetch_story(story_gid)
    subtype = story.get("resource_subtype")

    if subtype == "comment_added":
        _send_comment_alert(story, story_gid)
    elif subtype == "enum_custom_field_changed":
        _send_priority_field_changed_alert(story, story_gid)
    # 그 외 subtype(담당자 변경, 마감일 변경, 완료 처리 등)은 무시


def _send_comment_alert(story: dict, story_gid: str) -> None:
    task = story.get("target") or {}
    variables = {
        "태스크명": (task.get("name") or "")[:50],
        "작성자": (story.get("created_by") or {}).get("name", "익명"),
        "댓글내용": (story.get("text") or "")[:200],
        "asana_link": _strip_scheme(
            task.get("permalink_url") or f"https://app.asana.com/0/0/{task.get('gid', '')}"
        ),
    }
    if LOG_SENSITIVE_DETAILS:
        print("[댓글 알림] 발송 변수:", json.dumps(variables, ensure_ascii=False))
    else:
        print(f"[댓글 알림] 발송 (story_gid={story_gid})")
    send_to_all(SOLAPI_TEMPLATE_ID_COMMENT, variables)


def _send_priority_field_changed_alert(story: dict, story_gid: str) -> None:
    """기존 태스크의 우선순위 커스텀필드가 (다른 값 → High)로 바뀐 경우 발송합니다."""
    custom_field = story.get("custom_field") or {}
    new_value = story.get("new_enum_value") or {}

    if custom_field.get("name") != PRIORITY_FIELD_NAME:
        return  # 우리가 감시하는 우선순위 필드가 아닌 다른 단일선택 필드 변경이면 무시
    if new_value.get("name") != PRIORITY_HIGH_VALUE:
        return  # High로 "바뀐" 게 아니면(예: High→Medium, 혹은 다른 옵션 간 변경) 무시

    task_ref = story.get("target") or {}
    task_gid = task_ref.get("gid")
    if not task_gid:
        return

    task = fetch_task(task_gid)  # 담당자/마감일 등 최신 상세 정보 확보
    variables = {
        "태스크명": (task.get("name") or task_ref.get("name") or "")[:50],
        "담당자": (task.get("assignee") or {}).get("name", "미배정"),
        "마감일": task.get("due_on") or "마감일 미정",
        "asana_link": _strip_scheme(
            task.get("permalink_url") or task_ref.get("permalink_url") or f"https://app.asana.com/0/0/{task_gid}"
        ),
    }
    if LOG_SENSITIVE_DETAILS:
        print("[High 우선순위 알림-필드변경] 발송 변수:", json.dumps(variables, ensure_ascii=False))
    else:
        print(f"[High 우선순위 알림-필드변경] 발송 (task_gid={task_gid}, story_gid={story_gid})")
    send_to_all(SOLAPI_TEMPLATE_ID_HIGH_PRIORITY, variables)


def handle_task_added_event(event: dict) -> None:
    task_gid = event.get("resource", {}).get("gid")
    if not task_gid:
        return

    task = fetch_task(task_gid)
    priority_value = get_custom_field_value(task, PRIORITY_FIELD_NAME)
    if priority_value != PRIORITY_HIGH_VALUE:
        return  # High가 아니면 알림 발송 안 함

    variables = {
        "태스크명": (task.get("name") or "")[:50],
        "담당자": (task.get("assignee") or {}).get("name", "미배정"),
        "마감일": task.get("due_on") or "마감일 미정",
        "asana_link": _strip_scheme(task.get("permalink_url") or f"https://app.asana.com/0/0/{task_gid}"),
    }
    if LOG_SENSITIVE_DETAILS:
        print("[High 우선순위 알림] 발송 변수:", json.dumps(variables, ensure_ascii=False))
    else:
        print(f"[High 우선순위 알림] 발송 (task_gid={task_gid})")
    send_to_all(SOLAPI_TEMPLATE_ID_HIGH_PRIORITY, variables)


# ------------------------------------------------------------------
# 실행 (cron 등으로 1~5분마다 반복 호출)
# ------------------------------------------------------------------
if __name__ == "__main__":
    for var_name in REQUIRED_ENV_VARS:
        _require_env(var_name)

    events = fetch_events()
    print(f"[안내] 이벤트 {len(events)}건 수신" if events else "[안내] 새 이벤트 없음")

    # 한 배치 안에서 같은 리소스가 중복으로 잡히는 경우를 대비한 간단한 중복 제거
    seen_story_gids = set()
    seen_task_gids = set()

    for event in events:
        try:
            resource = event.get("resource", {})
            resource_type = resource.get("resource_type")
            action = event.get("action")
            gid = resource.get("gid")

            if resource_type == "story" and action == "added" and gid and gid not in seen_story_gids:
                seen_story_gids.add(gid)
                handle_story_event(event)
            elif resource_type == "task" and action == "added" and gid and gid not in seen_task_gids:
                seen_task_gids.add(gid)
                handle_task_added_event(event)
        except Exception as e:
            print(f"[이벤트 처리 오류] {event.get('resource', {}).get('gid')}: {e}")
