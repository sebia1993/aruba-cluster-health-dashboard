from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    user_message: str
    technical_message: str = ""

    def __str__(self) -> str:
        return self.user_message


ERROR_MESSAGES: dict[str, str] = {
    "AUTH_FAILED": "장비 로그인에 실패했습니다. 등록된 사용자 ID와 비밀번호를 확인하세요.",
    "TCP_TIMEOUT": "장비 연결 시간이 초과되었습니다. IP, 포트와 사내망 연결을 확인하세요.",
    "SSH_BANNER_MISSING": "SSH 응답을 확인하지 못했습니다. SSH 서비스와 장비 상태를 확인하세요.",
    "SSH_HOST_KEY_UNKNOWN": "승인되지 않은 SSH 호스트 키입니다. 설정에서 지문을 확인하세요.",
    "SSH_HOST_KEY_MISMATCH": "SSH 호스트 키가 이전 승인 값과 다릅니다. 장비 변경 여부를 확인하세요.",
    "PROMPT_NOT_FOUND": "장비 명령 프롬프트를 확인하지 못했습니다.",
    "COMMAND_TIMEOUT": "장비 명령 실행 시간이 초과되었습니다.",
    "PAGING_INCOMPLETE": "명령 출력의 페이징을 끝까지 처리하지 못했습니다.",
    "EMPTY_OUTPUT": "장비가 빈 명령 결과를 반환했습니다.",
    "OUTPUT_LIMIT_EXCEEDED": "명령 결과가 안전한 최대 크기를 초과했습니다.",
    "PARSE_HEADER_MISSING": "명령 결과에서 필요한 표 머리글을 찾지 못했습니다.",
    "PARSE_NO_VALID_ROWS": "명령 결과에서 유효한 장비 행을 찾지 못했습니다.",
    "SQLITE_BUSY": "로컬 상태 저장소가 사용 중입니다. 잠시 후 다시 시도하세요.",
    "SETTINGS_CORRUPT": "설정 파일을 읽을 수 없습니다. 백업 파일과 설정 내용을 확인하세요.",
    "CREDENTIAL_MISSING": "저장된 장비 자격 증명을 찾을 수 없습니다. 계정을 다시 입력하세요.",
    "NOTIFICATION_UNAVAILABLE": "Windows 알림을 표시하지 못했습니다. 알림 설정을 확인하세요.",
    "TRAY_UNAVAILABLE": "이 환경에서는 시스템 트레이를 사용할 수 없습니다.",
}


def app_error(code: str, technical_message: str = "") -> AppError:
    return AppError(code, ERROR_MESSAGES.get(code, "처리 중 오류가 발생했습니다."), technical_message)
