from __future__ import annotations

import base64
import json
import os
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .mail import GmailMessage

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_TOKEN_USERNAME = "gmail-readonly-token"


class GmailIntegrationError(RuntimeError):
    pass


class WindowsCredentialStore:
    def __init__(self, *, keyring_module=None, service_name: str = "JobPilot AI"):
        self.keyring_module = keyring_module
        self.service_name = service_name

    def _keyring(self):
        if self.keyring_module is not None:
            return self.keyring_module
        try:
            import keyring
        except ImportError as exc:
            raise GmailIntegrationError(
                "Install the Gmail optional dependencies to use secure token storage"
            ) from exc
        backend = keyring.get_keyring()
        if backend.priority <= 0:
            raise GmailIntegrationError(
                "No secure operating-system credential store is available"
            )
        if os.name == "nt" and type(backend).__name__ != "WinVaultKeyring":
            raise GmailIntegrationError(
                "Windows Credential Manager is required for Gmail token storage"
            )
        return keyring

    def get(self) -> str | None:
        keyring = self._keyring()
        return keyring.get_password(self.service_name, _TOKEN_USERNAME)

    def set(self, value: str) -> None:
        keyring = self._keyring()
        keyring.set_password(self.service_name, _TOKEN_USERNAME, value)


class GmailReadOnlyClient:
    def __init__(
        self,
        client_secret_file: str | Path,
        *,
        query: str = "newer_than:14d",
        max_results: int = 100,
        credential_store: WindowsCredentialStore | None = None,
        service=None,
    ):
        self.client_secret_file = Path(client_secret_file)
        self.query = query
        self.max_results = max(1, min(max_results, 500))
        self.credential_store = credential_store or WindowsCredentialStore()
        self.service = service

    def authorize(self, *, interactive: bool = False) -> None:
        if self.service is not None:
            return
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailIntegrationError(
                "Install the Gmail optional dependencies to authorize Gmail"
            ) from exc

        stored = self.credential_store.get()
        credentials = None
        if stored:
            credentials = Credentials.from_authorized_user_info(
                json.loads(stored), [GMAIL_READONLY_SCOPE]
            )
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.credential_store.set(credentials.to_json())
        elif not credentials or not credentials.valid:
            if not interactive:
                raise GmailIntegrationError(
                    "Gmail authorization is required; run `python main.py gmail auth` first"
                )
            if not self.client_secret_file.is_file():
                raise FileNotFoundError(
                    f"Missing local Gmail OAuth client file: {self.client_secret_file}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secret_file), [GMAIL_READONLY_SCOPE]
            )
            credentials = flow.run_local_server(port=0, open_browser=True)
            self.credential_store.set(credentials.to_json())

        self.service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )

    def fetch_messages(self) -> list[GmailMessage]:
        if self.service is None:
            raise GmailIntegrationError("Authorize Gmail before fetching messages")
        messages: list[GmailMessage] = []
        page_token: str | None = None
        while len(messages) < self.max_results:
            response = self.service.users().messages().list(
                userId="me",
                q=self.query,
                maxResults=min(100, self.max_results - len(messages)),
                pageToken=page_token,
            ).execute()
            items = response.get("messages", [])
            if not isinstance(items, list):
                raise GmailIntegrationError("Gmail returned an invalid message list")
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                payload = self.service.users().messages().get(
                    userId="me", id=item["id"], format="full"
                ).execute()
                messages.append(self._parse_message(payload))
                if len(messages) >= self.max_results:
                    break
            page_token = response.get("nextPageToken")
            if not page_token or not items:
                break
        return messages

    @classmethod
    def _parse_message(cls, payload: dict[str, Any]) -> GmailMessage:
        if not isinstance(payload.get("id"), str):
            raise GmailIntegrationError("Gmail message is missing its stable message ID")
        raw_headers = payload.get("payload", {}).get("headers", [])
        headers = {
            item.get("name", "").casefold(): item.get("value", "")
            for item in raw_headers if isinstance(item, dict)
        }
        received_at = cls._parse_date(headers.get("date", ""))
        body = cls._extract_plain_text(payload.get("payload", {}))[:12_000]
        try:
            subject = str(make_header(decode_header(headers.get("subject", ""))))
        except (LookupError, UnicodeDecodeError, ValueError) as exc:
            raise GmailIntegrationError("Gmail returned an invalid encoded subject") from exc
        return GmailMessage(
            message_id=payload["id"],
            sender=headers.get("from", ""),
            subject=subject,
            received_at=received_at,
            body=body or str(payload.get("snippet", ""))[:2_000],
        )

    @classmethod
    def _extract_plain_text(cls, payload: dict[str, Any]) -> str:
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data")
            if isinstance(data, str):
                padded = data + "=" * (-len(data) % 4)
                try:
                    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise GmailIntegrationError("Gmail returned malformed message content") from exc
        parts = payload.get("parts", [])
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    text = cls._extract_plain_text(part)
                    if text:
                        return text
        return ""

    @staticmethod
    def _parse_date(value: str) -> str | None:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
