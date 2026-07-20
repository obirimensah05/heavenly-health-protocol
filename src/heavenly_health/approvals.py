"""Owner-controlled, integrity-protected approval records for health mutations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4


class ApprovalError(RuntimeError):
    """An approval record is absent, invalid, expired, or in the wrong state."""


class ApprovalStore:
    """Persist signed proposals; only the local CLI exposes approve/reject actions."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._prepare_root()
        self._key = self._load_or_create_key()

    def propose_health_event(
        self,
        payload: Mapping[str, Any],
        *,
        preview: Mapping[str, Any],
        ttl: timedelta = timedelta(hours=24),
    ) -> dict[str, Any]:
        if ttl <= timedelta(0) or ttl > timedelta(days=7):
            raise ApprovalError("Proposal lifetime must be between zero and seven days")
        now = self._aware_now()
        approval_id = str(uuid4())
        record: dict[str, Any] = {
            "approval_id": approval_id,
            "operation": "insert_health_event",
            "status": "pending",
            "created_at": _timestamp(now),
            "expires_at": _timestamp(now + ttl),
            "preview": dict(preview),
            "payload": dict(payload),
        }
        self._write_record(record)
        return self._public_record(record)

    def propose_daily_feedback(
        self,
        *,
        daily_state: str,
        feedback: str,
        data_through: str | None,
        ttl: timedelta = timedelta(hours=24),
    ) -> dict[str, Any]:
        """Stage a user's outcome signal; local CLI approval makes it learnable.

        This deliberately records no raw health values. An MCP client can propose
        feedback but cannot mark it as owner-approved.
        """
        if daily_state not in {"recover", "maintain"}:
            raise ValueError("daily_state must be recover or maintain")
        if feedback not in {"done", "partly", "skipped", "not_useful"}:
            raise ValueError("feedback must be done, partly, skipped, or not_useful")
        if ttl <= timedelta(0) or ttl > timedelta(days=7):
            raise ApprovalError("Proposal lifetime must be between zero and seven days")
        now = self._aware_now()
        approval_id = str(uuid4())
        payload = {
            "daily_state": daily_state,
            "feedback": feedback,
            "reported_at": _timestamp(now),
            "data_through": data_through,
        }
        record: dict[str, Any] = {
            "approval_id": approval_id,
            "operation": "record_daily_feedback",
            "status": "pending",
            "created_at": _timestamp(now),
            "expires_at": _timestamp(now + ttl),
            "preview": {
                "operation": "record_daily_feedback",
                "daily_state": daily_state,
                "feedback": feedback,
                "data_through": data_through,
                "confirmation": "Run heavenly approval approve <approval-id> locally",
            },
            "payload": payload,
        }
        self._write_record(record)
        return self._public_record(record)

    def feedback_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return only locally approved, compact feedback suitable for later learning."""
        history: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                record = self._read_record(path.stem)
            except ApprovalError:
                continue
            if record.get("operation") != "record_daily_feedback" or record.get("status") != "approved":
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            history.append(
                {
                    "daily_state": payload.get("daily_state"),
                    "feedback": payload.get("feedback"),
                    "reported_at": payload.get("reported_at"),
                    "data_through": payload.get("data_through"),
                }
            )
        history.sort(key=lambda item: str(item.get("reported_at", "")), reverse=True)
        return history[: max(1, min(int(limit), 200))]

    def get(self, approval_id: str) -> dict[str, Any]:
        return self._read_record(approval_id)

    def review(self, approval_id: str) -> dict[str, Any]:
        """Return preview and state only, never the executable mutation payload."""
        return self._public_record(self._read_record(approval_id))

    def approve(self, approval_id: str) -> dict[str, Any]:
        record = self._read_record(approval_id)
        self._require_not_expired(record)
        status = record.get("status")
        if status != "pending":
            raise ApprovalError(f"Proposal cannot be approved from status: {status}")
        record["status"] = "approved"
        record["approved_at"] = _timestamp(self._aware_now())
        self._write_record(record)
        return self._public_record(record)

    def reject(self, approval_id: str) -> dict[str, Any]:
        record = self._read_record(approval_id)
        status = record.get("status")
        if status not in {"pending", "approved"}:
            raise ApprovalError(f"Proposal cannot be rejected from status: {status}")
        record["status"] = "rejected"
        record["rejected_at"] = _timestamp(self._aware_now())
        self._write_record(record)
        return self._public_record(record)

    def consume_approved(self, approval_id: str) -> dict[str, Any]:
        record = self._read_record(approval_id)
        status = record.get("status")
        if status == "pending":
            raise ApprovalError("Proposal is not owner-approved")
        if status == "rejected":
            raise ApprovalError("Proposal was rejected")
        if status == "executed":
            raise ApprovalError("Proposal was already executed")
        if status == "executing":
            raise ApprovalError("Proposal execution is already in progress")
        if status != "approved":
            raise ApprovalError("Proposal is not executable")
        self._require_not_expired(record)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ApprovalError("Proposal payload is invalid")
        executable = dict(payload)
        executable["source_record_id"] = f"heavenly-proposal:{approval_id}"
        record["status"] = "executing"
        record["execution_started_at"] = _timestamp(self._aware_now())
        self._write_record(record)
        return {"approval_id": approval_id, "payload": executable}

    def mark_executed(self, approval_id: str, *, result_reference: str) -> None:
        record = self._read_record(approval_id)
        if record.get("status") != "executing":
            raise ApprovalError("Only an executing proposal can be marked executed")
        record["status"] = "executed"
        record["executed_at"] = _timestamp(self._aware_now())
        record["result_reference"] = result_reference
        self._write_record(record)

    def release_failed_execution(self, approval_id: str) -> None:
        record = self._read_record(approval_id)
        if record.get("status") != "executing":
            raise ApprovalError("Only an executing proposal can be released")
        record["status"] = "approved"
        record["last_execution_failed_at"] = _timestamp(self._aware_now())
        self._write_record(record)

    def audit_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                record = self._read_record(path.stem)
            except ApprovalError:
                continue
            records.append(self._public_record(record))
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records[: max(1, min(int(limit), 200))]

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise ApprovalError("Approval directory must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError as exc:
            raise ApprovalError("Approval directory must be owner-only") from exc
        metadata = self.root.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ApprovalError("Approval directory must be owner-only")

    def _load_or_create_key(self) -> bytes:
        path = self.root / ".signing-key"
        if path.is_symlink():
            raise ApprovalError("Approval signing key must not be a symbolic link")
        if not path.exists():
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                key = secrets.token_bytes(32)
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        try:
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ApprovalError("Approval signing key must be owner-only")
            key = path.read_bytes()
        except OSError as exc:
            raise ApprovalError("Approval signing key is unavailable") from exc
        if len(key) != 32:
            raise ApprovalError("Approval signing key is invalid")
        return key

    def _read_record(self, approval_id: str) -> dict[str, Any]:
        normalized_id = _approval_id(approval_id)
        path = self.root / f"{normalized_id}.json"
        if path.is_symlink():
            raise ApprovalError("Approval record must not be a symbolic link")
        try:
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ApprovalError("Approval record must be owner-only")
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApprovalError("Unknown approval ID") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ApprovalError("Approval record is unreadable") from exc
        if not isinstance(record, dict) or record.get("approval_id") != normalized_id:
            raise ApprovalError("Approval record is invalid")
        signature = record.pop("signature", None)
        expected = self._signature(record)
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ApprovalError("Approval record integrity check failed")
        return record

    def _write_record(self, record: Mapping[str, Any]) -> None:
        approval_id = _approval_id(str(record.get("approval_id", "")))
        payload = dict(record)
        payload["signature"] = self._signature(payload)
        target = self.root / f"{approval_id}.json"
        temporary = self.root / f".{approval_id}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                descriptor = -1
                json.dump(payload, file, sort_keys=True, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _signature(self, record: Mapping[str, Any]) -> str:
        unsigned = {key: value for key, value in record.items() if key != "signature"}
        encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._key, encoded, hashlib.sha256).hexdigest()

    def _require_not_expired(self, record: Mapping[str, Any]) -> None:
        expires_at = record.get("expires_at")
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApprovalError("Proposal expiry is invalid") from exc
        if self._aware_now() >= expires:
            raise ApprovalError("Proposal has expired")

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalError("Approval clock must return a timezone-aware timestamp")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"payload", "signature"}
        }


def _approval_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApprovalError("Unknown approval ID") from exc


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def approval_state_path(environ: Mapping[str, str]) -> Path:
    """Return an explicit absolute path or the private native state default."""
    configured = environ.get("HEAVENLY_APPROVAL_STATE_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ApprovalError("HEAVENLY_APPROVAL_STATE_DIR must be an absolute path")
        return path
    state_home = environ.get("XDG_STATE_HOME", "").strip()
    if state_home:
        return Path(state_home).expanduser() / "heavenly" / "approvals"
    return Path.home() / ".local" / "state" / "heavenly" / "approvals"
