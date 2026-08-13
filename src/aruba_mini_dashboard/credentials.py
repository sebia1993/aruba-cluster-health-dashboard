"""Credential storage with Windows Credential Manager and session-only modes."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


CREDENTIAL_TARGET_PREFIX = "ArubaMiniDashboard/"
MAX_CREDENTIAL_BLOB_BYTES = 2560
MAX_CREDENTIAL_USERNAME_CHARS = 513
_CREDENTIAL_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


class CredentialError(RuntimeError):
    """Base class for credential-store failures with sanitized messages."""


class CredentialNotFoundError(CredentialError):
    pass


class CredentialStoreUnavailableError(CredentialError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class DeviceCredential:
    username: str
    password: str
    enable_secret: str = ""

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (self.username, self.password, self.enable_secret)):
            raise ValueError("자격 증명 값 형식이 올바르지 않습니다.")
        if not self.username.strip():
            raise ValueError("사용자 ID는 비워 둘 수 없습니다.")
        if len(self.username) > MAX_CREDENTIAL_USERNAME_CHARS:
            raise ValueError("사용자 ID가 Windows 저장 한도를 초과했습니다.")
        if not self.password:
            raise ValueError("비밀번호는 비워 둘 수 없습니다.")

    def __repr__(self) -> str:
        return "DeviceCredential(username='[REDACTED]', password='[REDACTED]', enable_secret='[REDACTED]')"


class CredentialStore(Protocol):
    def save(self, credential_id: str, credential: DeviceCredential) -> str: ...

    def get(self, credential_id: str) -> DeviceCredential: ...

    def delete(self, credential_id: str) -> None: ...

    def clear(self) -> None: ...


def new_credential_id() -> str:
    return str(uuid.uuid4())


def validate_credential_id(credential_id: str) -> str:
    candidate = str(credential_id).strip()
    if not _CREDENTIAL_ID.fullmatch(candidate):
        raise ValueError("자격 증명 식별자 형식이 올바르지 않습니다.")
    return candidate.lower()


def credential_target(credential_id: str) -> str:
    return f"{CREDENTIAL_TARGET_PREFIX}{validate_credential_id(credential_id)}"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite credential value")


class SessionCredentialStore:
    """In-memory store.  Values disappear when this object/process exits."""

    def __init__(self) -> None:
        self._credentials: dict[str, DeviceCredential] = {}
        self._lock = threading.RLock()

    def save(self, credential_id: str, credential: DeviceCredential) -> str:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            self._credentials[normalized] = credential
        return normalized

    def create(self, credential: DeviceCredential) -> str:
        return self.save(new_credential_id(), credential)

    def get(self, credential_id: str) -> DeviceCredential:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            try:
                return self._credentials[normalized]
            except KeyError as exc:
                raise CredentialNotFoundError("세션 전용 자격 증명을 찾을 수 없습니다.") from exc

    def delete(self, credential_id: str) -> None:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            self._credentials.pop(normalized, None)

    def clear(self) -> None:
        with self._lock:
            self._credentials.clear()


class WindowsCredentialStore:
    """Generic Credential wrapper loaded lazily so non-Windows tests can run."""

    def __init__(self, win32cred_module: object | None = None) -> None:
        self._module = win32cred_module

    def _api(self):
        if self._module is not None:
            return self._module
        try:
            import win32cred
        except (ImportError, OSError) as exc:
            raise CredentialStoreUnavailableError(
                "Windows Credential Manager를 사용할 수 없습니다. 세션 전용 자격 증명을 사용하세요."
            ) from exc
        self._module = win32cred
        return win32cred

    def save(self, credential_id: str, credential: DeviceCredential) -> str:
        normalized = validate_credential_id(credential_id)
        api = self._api()
        if len(credential.password) + len(credential.enable_secret) > MAX_CREDENTIAL_BLOB_BYTES // 2:
            raise CredentialError("자격 증명 값이 Windows 저장 한도를 초과했습니다.")
        blob = json.dumps(
            {"password": credential.password, "enable_secret": credential.enable_secret},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # Windows generic credential blobs are deliberately small; reject
        # instead of silently truncating authentication material.
        if len(blob.encode("utf-16-le")) > MAX_CREDENTIAL_BLOB_BYTES:
            raise CredentialError("자격 증명 값이 Windows 저장 한도를 초과했습니다.")
        record = {
            "Type": api.CRED_TYPE_GENERIC,
            "TargetName": credential_target(normalized),
            "UserName": credential.username,
            "CredentialBlob": blob,
            "Persist": api.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "Aruba Mini Dashboard device credential",
        }
        try:
            api.CredWrite(record, 0)
        except Exception as exc:
            raise CredentialStoreUnavailableError("Windows Credential Manager에 저장하지 못했습니다.") from exc
        return normalized

    def create(self, credential: DeviceCredential) -> str:
        return self.save(new_credential_id(), credential)

    def get(self, credential_id: str) -> DeviceCredential:
        normalized = validate_credential_id(credential_id)
        api = self._api()
        try:
            record = api.CredRead(credential_target(normalized), api.CRED_TYPE_GENERIC, 0)
        except Exception as exc:
            error_code = getattr(exc, "winerror", None)
            if error_code in {1168, 2}:
                raise CredentialNotFoundError("저장된 장비 자격 증명을 찾을 수 없습니다.") from exc
            raise CredentialStoreUnavailableError("Windows Credential Manager에서 읽지 못했습니다.") from exc
        try:
            if not isinstance(record, Mapping):
                raise TypeError("credential record must be a mapping")
            username = record.get("UserName", "")
            if type(username) is not str or len(username) > MAX_CREDENTIAL_USERNAME_CHARS:
                raise TypeError("credential username must be text")
            raw_blob = record.get("CredentialBlob", b"")
            if isinstance(raw_blob, str):
                if len(raw_blob) > MAX_CREDENTIAL_BLOB_BYTES // 2:
                    raise ValueError("credential blob exceeds the Windows limit")
                if len(raw_blob.encode("utf-16-le")) > MAX_CREDENTIAL_BLOB_BYTES:
                    raise ValueError("credential blob exceeds the Windows limit")
                payload = json.loads(
                    raw_blob,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            else:
                if not isinstance(raw_blob, (bytes, bytearray, memoryview)):
                    raise TypeError("credential blob must be text or bytes")
                if len(raw_blob) > MAX_CREDENTIAL_BLOB_BYTES:
                    raise ValueError("credential blob exceeds the Windows limit")
                encoded = bytes(raw_blob)
                # pywin32's Unicode CredWrite accepts str and returns the
                # credential blob as UTF-16LE bytes. Retain UTF-8 support for
                # compatibility with alternate/fake APIs and old tests.
                encodings = ("utf-16-le", "utf-8") if b"\x00" in encoded else ("utf-8", "utf-16-le")
                decode_error: Exception | None = None
                for encoding in encodings:
                    try:
                        payload = json.loads(
                            encoded.decode(encoding),
                            object_pairs_hook=_unique_json_object,
                            parse_constant=_reject_json_constant,
                        )
                        break
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        decode_error = exc
                else:
                    raise ValueError("unsupported credential blob encoding") from decode_error
            if type(payload) is not dict:
                raise TypeError("credential payload must be an object")
            if set(payload) - {"password", "enable_secret"}:
                raise TypeError("credential payload contains unsupported fields")
            password = payload.get("password")
            enable_secret = payload.get("enable_secret", "")
            if type(password) is not str or type(enable_secret) is not str:
                raise TypeError("credential payload fields must be text")
            return DeviceCredential(
                username=username,
                password=password,
                enable_secret=enable_secret,
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            # Do not retain or log a cause whose representation could contain a
            # malformed credential blob supplied by an alternate provider.
            raise CredentialError("저장된 자격 증명 형식이 손상되었습니다.") from None

    def delete(self, credential_id: str) -> None:
        normalized = validate_credential_id(credential_id)
        api = self._api()
        try:
            api.CredDelete(credential_target(normalized), api.CRED_TYPE_GENERIC, 0)
        except Exception as exc:
            if getattr(exc, "winerror", None) not in {1168, 2}:
                raise CredentialStoreUnavailableError("Windows Credential Manager에서 삭제하지 못했습니다.") from exc

    def clear(self) -> None:
        """Do not enumerate/delete unrelated credentials implicitly."""


class CredentialService:
    """Resolve opaque identifiers from persistent or session-only stores."""

    def __init__(
        self,
        persistent: CredentialStore | None = None,
        session: SessionCredentialStore | None = None,
    ) -> None:
        self.persistent = persistent if persistent is not None else WindowsCredentialStore()
        self.session = session if session is not None else SessionCredentialStore()
        self._session_ids: set[str] = set()
        self._lock = threading.RLock()

    def save(self, credential: DeviceCredential, *, session_only: bool, credential_id: str | None = None) -> str:
        identifier = validate_credential_id(credential_id) if credential_id else new_credential_id()
        with self._lock:
            if session_only:
                self.session.save(identifier, credential)
                self._session_ids.add(identifier)
            else:
                self.persistent.save(identifier, credential)
                self._session_ids.discard(identifier)
                self.session.delete(identifier)
        return identifier

    def get(self, credential_id: str) -> DeviceCredential:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            if normalized in self._session_ids:
                return self.session.get(normalized)
            return self.persistent.get(normalized)

    def is_session(self, credential_id: str) -> bool:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            return normalized in self._session_ids

    def delete(self, credential_id: str) -> None:
        normalized = validate_credential_id(credential_id)
        with self._lock:
            if normalized in self._session_ids:
                self.session.delete(normalized)
                self._session_ids.discard(normalized)
                return
            self.persistent.delete(normalized)

    def close(self) -> None:
        with self._lock:
            self.session.clear()
            self._session_ids.clear()

    def __enter__(self) -> "CredentialService":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
