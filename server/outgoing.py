"""Durable, single-attempt sends. An ambiguous attempt is never retried.

Text still goes out through KakaoTalk's notification reply. Photos, files, video,
and audio are staged for an ACTION_SEND intent. Quote replies, owned emoticons,
and locations need the on-device UI driver and stay disabled unless
KAKAO_UI_ACTIONS is on.
"""

import base64
import hashlib
import json
import os
import time
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# One attachment, and the whole request. Base64 is rejected before decode by max_length.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 10

# MIME → (kind, canonical extension). The filename extension must agree.
MEDIA_TYPES = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "image/webp": ("image", "webp"),
    "image/heic": ("image", "heic"),
    "image/heif": ("image", "heif"),
    "video/mp4": ("video", "mp4"),
    "video/3gpp": ("video", "3gp"),
    "video/webm": ("video", "webm"),
    "video/quicktime": ("video", "mov"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp4": ("audio", "m4a"),
    "audio/aac": ("audio", "aac"),
    "audio/3gpp": ("audio", "3gp"),
    "audio/amr": ("audio", "amr"),
    "audio/ogg": ("audio", "ogg"),
    "audio/wav": ("audio", "wav"),
    "application/pdf": ("file", "pdf"),
    "application/zip": ("file", "zip"),
    "text/plain": ("file", "txt"),
    "application/octet-stream": ("file", "bin"),
}
EXTENSIONS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"},
    "video": {"mp4", "3gp", "webm", "mov"},
    "audio": {"mp3", "m4a", "aac", "3gp", "amr", "ogg", "wav"},
    "file": {"pdf", "zip", "txt", "bin"},
}
# Collected chat_logs.type values that count as delivery of each send mode.
EXPECTED_TYPES = {
    "text": ["1"],
    "reply": ["26"],
    "emoticon": ["6", "12", "20", "22", "25"],
    # 26.9.1 map UI produced Universal 100. Classic Location 16 is accepted too.
    "location": ["100", "16"],
}
FAIL_REASONS = {
    "device_rejected",
    "screen_locked",
    "screen_state_unreadable",
    "screen_busy",
    "ui_actions_disabled",
    "ui_action_timeout",
    "ui_action_failed",
    "media_stage_failed",
    "kakao_activity_rejected",
    "keyboard_unavailable",
}


def ui_actions_enabled(environ=None):
    value = (environ if environ is not None else os.environ).get("KAKAO_UI_ACTIONS", "")
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def clean_text(value):
    if value is None:
        return None
    if not value.strip() or "\0" in value or len(value.encode("utf-16-le")) > 8000:
        raise ValueError("invalid_text")
    return value


def _control_free(value, limit):
    if any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ValueError("invalid_text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        raise ValueError("invalid_text")
    return cleaned


def safe_attachment_name(raw, kind):
    name = raw.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or name.startswith("."):
        return None
    if any(ord(ch) < 32 or ch in '/\\:*?"<>|' for ch in name):
        return None
    if len(name) > 60 or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in EXTENSIONS[kind]:
        return None
    return name


def decode_attachments(items):
    files, total, kinds = [], 0, set()
    for ordinal, item in enumerate(items):
        try:
            raw = base64.b64decode(item.data_base64, validate=True)
        except (ValueError, TypeError):
            raise ValueError("invalid_attachment") from None
        if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment_too_large" if raw else "invalid_attachment")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("attachment_too_large")
        spec = MEDIA_TYPES.get(item.mime.lower())
        if spec is None:
            raise ValueError("unsupported_media_type")
        kind, _ext = spec
        display = safe_attachment_name(item.name, kind)
        if display is None:
            raise ValueError("invalid_attachment_name")
        staged = f"kb-{ordinal}-{display}"
        if len(staged) > 80 or not staged.startswith("kb-"):
            raise ValueError("invalid_attachment_name")
        kinds.add(kind)
        files.append(
            {
                "ordinal": ordinal,
                "name": staged,
                "display_name": display,
                "mime": item.mime.lower(),
                "kind": kind,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "bytes": raw,
            }
        )
    if len(kinds) > 1:
        raise ValueError("mixed_attachment_kinds")
    return files


def expected_types(mode, kind=None, count=1):
    if mode == "media":
        if kind == "image":
            return ["2", "27"] if count > 1 else ["2"]
        return {"video": ["3"], "audio": ["5"], "file": ["18"]}[kind]
    return list(EXPECTED_TYPES[mode])


class OutgoingAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1, max_length=120)]
    mime: Annotated[str, Field(min_length=1, max_length=127)]
    data_base64: Annotated[str, Field(min_length=4, max_length=12_000_000)]


def _json_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # BeforeValidator must raise ValueError so strict JSON ints stay a 422.
        raise ValueError("location_needs_coordinates")  # noqa: TRY004
    return float(value)


class OutgoingRequest(BaseModel):
    """One `/v1/outgoing` body. Exactly one of text, attachments, reply, emoticon, location."""

    model_config = ConfigDict(extra="forbid", strict=True)
    _mode: str = PrivateAttr(default="text")
    _files: list = PrivateAttr(default_factory=list)
    request_id: Annotated[str, Field(description="A new UUID per intended message; reuse on retry.")]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=256)]
    text: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    thread_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")] | None = None
    reply_to: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")] | None = None
    attachments: list[OutgoingAttachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    favorite_index: Annotated[int, Field(ge=0, le=29)] | None = None
    latitude: Annotated[float, BeforeValidator(_json_float), Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, BeforeValidator(_json_float), Field(ge=-180, le=180)] | None = None
    location_title: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    location_address: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    @field_validator("request_id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        return clean_text(value)

    @field_validator("location_title", "location_address")
    @classmethod
    def valid_label(cls, value):
        return None if value is None else _control_free(value, 120)

    @model_validator(mode="after")
    def one_mode(self):
        media = bool(self.attachments)
        emoticon = self.favorite_index is not None
        location = any(
            value is not None
            for value in (self.latitude, self.longitude, self.location_title, self.location_address)
        )
        reply = self.reply_to is not None
        selected = [
            name
            for name, enabled in (
                ("media", media),
                ("emoticon", emoticon),
                ("location", location),
                ("reply", reply),
            )
            if enabled
        ]
        if len(selected) > 1:
            raise ValueError("conflicting_send_fields")
        mode = selected[0] if selected else "text"
        if mode == "text":
            if self.text is None:
                raise ValueError("invalid_text")
        elif mode == "reply":
            if self.text is None:
                raise ValueError("invalid_text")
            if self.thread_id is not None:
                raise ValueError("thread_id_not_supported_for_reply")
        elif mode == "media":
            if self.text is not None or self.thread_id is not None:
                raise ValueError("media_send_is_attachments_only")
        elif self.text is not None or self.thread_id is not None:
            raise ValueError("ui_send_has_no_text")
        if location and (self.latitude is None or self.longitude is None):
            raise ValueError("location_needs_coordinates")
        self._mode = mode
        self._files = decode_attachments(self.attachments) if media else []
        return self

    @property
    def mode(self):
        return self._mode

    @property
    def files(self):
        return self._files

    def location_record(self):
        if self.mode != "location":
            return None
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "title": self.location_title,
            "address": self.location_address,
        }


class SendMessage(BaseModel):
    """Text tool. `thread_id` is the notification thread. `reply_to` is a quote (UI)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field(description="A new UUID per intended message; reuse on retry.")]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=256)]
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    thread_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")] | None = None
    reply_to: Annotated[
        str,
        Field(
            pattern=r"^[1-9][0-9]{0,18}$",
            description="KakaoTalk chat log id (log_id) to quote. Requires KAKAO_UI_ACTIONS.",
        ),
    ] | None = None

    @field_validator("request_id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        return clean_text(value)


class SendMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field()]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=256)]
    attachments: list[OutgoingAttachment] = Field(min_length=1, max_length=MAX_ATTACHMENTS)

    _valid_id = field_validator("request_id")(SendMessage.valid_id.__func__)


class SendEmoticon(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field()]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=256)]
    favorite_index: Annotated[int, Field(ge=0, le=29)] = 0

    _valid_id = field_validator("request_id")(SendMessage.valid_id.__func__)


class SendLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field()]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=256)]
    latitude: Annotated[float, BeforeValidator(_json_float), Field(ge=-90, le=90)]
    longitude: Annotated[float, BeforeValidator(_json_float), Field(ge=-180, le=180)]
    location_title: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    location_address: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    _valid_id = field_validator("request_id")(SendMessage.valid_id.__func__)

    @field_validator("location_title", "location_address")
    @classmethod
    def valid_label(cls, value):
        return None if value is None else _control_free(value, 120)


class SendStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str

    _valid_id = field_validator("request_id")(SendMessage.valid_id.__func__)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_epoch: UUID
    account_ref: UUID
    database_id: str = Field(min_length=1, max_length=128)


class SendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["submitted", "failed", "unknown"]
    match_after_seq: int | None = Field(default=None, ge=0, le=9223372036854775807)
    reason: str | None = Field(default=None, max_length=64)


class SendError(Exception):
    def __init__(self, reason, code=409):
        self.reason, self.code = reason, code


def _as_request(body):
    if isinstance(body, OutgoingRequest):
        return body
    data = body.model_dump(exclude_none=True) if hasattr(body, "model_dump") else body
    return OutgoingRequest.model_validate(data)


def _digest(body: OutgoingRequest):
    # Plain text keeps the historical digest so an in-flight retry still matches.
    if body.mode == "text" and body.thread_id is None:
        return hashlib.sha256(
            json.dumps([body.conversation_ref, body.text], ensure_ascii=False).encode()
        ).hexdigest()
    payload = {
        "attachments": [
            {"mime": item["mime"], "name": item["name"], "sha256": item["sha256"]}
            for item in body.files
        ],
        "conversation_ref": body.conversation_ref,
        "favorite_index": body.favorite_index,
        "location": body.location_record(),
        "mode": body.mode,
        "reply_to": body.reply_to,
        "text": body.text,
        "thread_id": body.thread_id,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def scrub_restored_queue(db):
    """Drop queued attachment bytes when a backup is restored. Old snapshots lack the table."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "outgoing" not in tables:
        return
    cols = {row[1] for row in db.execute("PRAGMA table_info(outgoing)")}
    if "outgoing_blobs" in tables and "request_id" in cols:
        db.execute(
            "DELETE FROM outgoing_blobs WHERE request_id IN "
            "(SELECT request_id FROM outgoing WHERE status IN ('queued','dispatching'))"
        )
    assignments = []
    if "reply_preview" in cols:
        assignments.append("reply_preview=NULL")
    if "location_json" in cols:
        assignments.append("location_json=NULL")
    if assignments:
        db.execute(
            "UPDATE outgoing SET "
            + ",".join(assignments)
            + " WHERE status IN ('queued','dispatching')"
        )


class Outgoing:
    def __init__(self, store, config):
        self.store, self.config = store, config
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outgoing (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    conversation_ref TEXT NOT NULL, device_id TEXT NOT NULL,
                    epoch TEXT NOT NULL, database_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    text TEXT, status TEXT NOT NULL, reason TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, account_ref TEXT,
                    kind TEXT NOT NULL DEFAULT 'text', thread_id TEXT,
                    expected_types TEXT NOT NULL DEFAULT '["1"]',
                    match_after_seq INTEGER, matched_message_id INTEGER,
                    reply_to TEXT, reply_preview TEXT, emoticon_index INTEGER,
                    location_json TEXT
                );
                CREATE INDEX IF NOT EXISTS outgoing_pending ON outgoing(status, created_at);
                CREATE INDEX IF NOT EXISTS outgoing_created ON outgoing(created_at);
                CREATE TABLE IF NOT EXISTS outgoing_blobs (
                    request_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    name TEXT NOT NULL, mime TEXT NOT NULL, kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, bytes BLOB NOT NULL,
                    PRIMARY KEY (request_id, ordinal)
                );
            """)
            cols = {row[1] for row in db.execute("PRAGMA table_info(outgoing)")}
            additions = {
                "kind": "ALTER TABLE outgoing ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'",
                "thread_id": "ALTER TABLE outgoing ADD COLUMN thread_id TEXT",
                "expected_types": "ALTER TABLE outgoing ADD COLUMN expected_types TEXT NOT NULL DEFAULT '[\"1\"]'",
                "match_after_seq": "ALTER TABLE outgoing ADD COLUMN match_after_seq INTEGER",
                "matched_message_id": "ALTER TABLE outgoing ADD COLUMN matched_message_id INTEGER",
                "reply_to": "ALTER TABLE outgoing ADD COLUMN reply_to TEXT",
                "reply_preview": "ALTER TABLE outgoing ADD COLUMN reply_preview TEXT",
                "emoticon_index": "ALTER TABLE outgoing ADD COLUMN emoticon_index INTEGER",
                "location_json": "ALTER TABLE outgoing ADD COLUMN location_json TEXT",
            }
            for name, statement in additions.items():
                if name not in cols:
                    db.execute(statement)

    @staticmethod
    def _drop_finished_blobs(db):
        db.execute(
            "DELETE FROM outgoing_blobs WHERE request_id NOT IN "
            "(SELECT request_id FROM outgoing WHERE status IN ('queued','dispatching'))"
        )

    def expire(self, db):
        # Clear content on expiry; retain IDs/digests to reject late duplicate requests.
        db.execute(
            "UPDATE outgoing SET status='failed', reason='queue_expired', text=NULL, "
            "reply_preview=NULL, location_json=NULL, updated_at=? "
            "WHERE status='queued' AND created_at<?",
            (time.time(), time.time() - 120),
        )
        db.execute(
            "UPDATE outgoing SET status='unknown', reason='attempt_result_unavailable', "
            "text=NULL, reply_preview=NULL, location_json=NULL, updated_at=? "
            "WHERE status='dispatching' AND updated_at<?",
            (time.time(), time.time() - 120),
        )
        self._drop_finished_blobs(db)

    def gate(self, db):
        row = db.execute("SELECT * FROM statuses WHERE kind='bridge'").fetchone()
        bridge = json.loads(row["body"]) if row else {}
        if (
            not row
            or time.time() - datetime.fromisoformat(row["received_at"]).timestamp()
            > self.config.heartbeat_timeout
            or bridge.get("device_id") != self.config.device_id
            or bridge.get("source") != "iris_db"
            or not bridge.get("listener_connected")
            or not bridge.get("secondary_login_confirmed")
            or not bridge.get("supports_message_send")
            or not bridge.get("account_ref")
        ):
            raise SendError("sending_unavailable_confirm_login_and_upgrade_iris", 423)
        return bridge

    @staticmethod
    def view(row):
        matched = row["matched_message_id"]
        return {
            key: row[key]
            for key in (
                "request_id",
                "conversation_ref",
                "status",
                "reason",
                "created_at",
                "updated_at",
            )
        } | {
            "delivery_confirmed": matched is not None,
            "matched_message_id": matched,
            "kind": row["kind"],
        }

    def _reply_preview(self, db, body):
        row = db.execute(
            """SELECT c.body FROM message_lookup m
               JOIN candidates c ON c.id=m.message_id
               JOIN observations o ON o.id=c.observation_id
               WHERE m.conversation_ref=? AND o.source_seq=? AND m.source='iris_db'
               LIMIT 1""",
            (body.conversation_ref, int(body.reply_to)),
        ).fetchone()
        if row is None:
            raise SendError("reply_source_not_found")
        preview = (row["body"] or "").strip()
        if not preview or "\0" in preview:
            raise SendError("reply_source_has_no_visible_text")
        return preview[:200]

    def enqueue(self, body):
        body = _as_request(body)
        digest = _digest(body)
        kind = body.files[0]["kind"] if body.mode == "media" else None
        types = expected_types(body.mode, kind, len(body.files) or 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            prior = db.execute(
                "SELECT * FROM outgoing WHERE request_id=?", (body.request_id,)
            ).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise SendError("request_id_conflict")
                return self.view(prior)
            if body.mode in {"reply", "emoticon", "location"} and not ui_actions_enabled():
                raise SendError("ui_actions_disabled", 403)
            bridge = self.gate(db)
            if bridge.get("notification_reply_ready") is False:
                # Unknown does not block: Iris checks the referer again before sending.
                raise SendError("sending_unavailable_until_kakaotalk_notification", 423)
            target = db.execute(
                "SELECT * FROM message_lookup WHERE conversation_ref=? "
                "ORDER BY message_id DESC LIMIT 1",
                (body.conversation_ref,),
            ).fetchone()
            if not target or any(
                target[key] != value
                for key, value in {
                    "device_id": self.config.device_id,
                    "epoch": bridge["enrollment_epoch"],
                    "database_id": bridge["database_id"],
                    "source": "iris_db",
                }.items()
            ):
                raise SendError("conversation_not_in_current_enrollment")
            preview = self._reply_preview(db, body) if body.mode == "reply" else None
            if (
                db.execute(
                    "SELECT count(*) FROM outgoing WHERE created_at>?", (time.time() - 60,)
                ).fetchone()[0]
                >= 30
            ):
                raise SendError("send_rate_limited", 429)
            instant = time.time()
            location = json.dumps(body.location_record(), ensure_ascii=False) if body.location_record() else None
            db.execute(
                "INSERT INTO outgoing(request_id,digest,conversation_ref,device_id,epoch,"
                "database_id,chat_id,text,status,reason,created_at,updated_at,account_ref,"
                "kind,thread_id,expected_types,reply_to,reply_preview,emoticon_index,location_json) "
                "VALUES(?,?,?,?,?,?,?,?,'queued',NULL,?,?,?,?,?,?,?,?,?,?)",
                (
                    body.request_id,
                    digest,
                    body.conversation_ref,
                    target["device_id"],
                    target["epoch"],
                    target["database_id"],
                    target["chat_id"],
                    body.text,
                    instant,
                    instant,
                    bridge["account_ref"],
                    body.mode,
                    body.thread_id,
                    json.dumps(types),
                    body.reply_to,
                    preview,
                    body.favorite_index,
                    location,
                ),
            )
            for item in body.files:
                db.execute(
                    "INSERT INTO outgoing_blobs(request_id,ordinal,name,mime,kind,sha256,size,bytes) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        body.request_id,
                        item["ordinal"],
                        item["name"],
                        item["mime"],
                        item["kind"],
                        item["sha256"],
                        item["size"],
                        item["bytes"],
                    ),
                )
            return self.view(
                db.execute(
                    "SELECT * FROM outgoing WHERE request_id=?", (body.request_id,)
                ).fetchone()
            )

    def confirm_all(self):
        """Pair each submitted send with the earliest later is_mine row of the expected type."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT request_id, conversation_ref, expected_types, match_after_seq "
                "FROM outgoing WHERE status='submitted' AND matched_message_id IS NULL "
                "AND match_after_seq IS NOT NULL ORDER BY created_at, request_id"
            ).fetchall()
            used = {
                row[0]
                for row in db.execute(
                    "SELECT matched_message_id FROM outgoing WHERE matched_message_id IS NOT NULL"
                )
            }
            now = time.time()
            for item in pending:
                try:
                    types = json.loads(item["expected_types"])
                except (TypeError, ValueError):
                    continue
                if not types:
                    continue
                placeholders = ",".join("?" * len(types))
                rows = db.execute(
                    f"""SELECT c.id, o.source_seq FROM message_lookup m
                        JOIN candidates c ON c.id=m.message_id
                        JOIN observations o ON o.id=c.observation_id
                        WHERE m.conversation_ref=? AND m.is_mine=1 AND m.source='iris_db'
                          AND o.source_seq>? AND m.message_type IN ({placeholders})
                        ORDER BY o.source_seq ASC""",
                    (item["conversation_ref"], item["match_after_seq"], *types),
                ).fetchall()
                match = next((row for row in rows if row["id"] not in used), None)
                if match is None:
                    continue
                used.add(match["id"])
                db.execute(
                    "UPDATE outgoing SET matched_message_id=?, reason='matched_collected_message', "
                    "updated_at=? WHERE request_id=? AND matched_message_id IS NULL",
                    (match["id"], now, item["request_id"]),
                )

    def status(self, request_id):
        self.confirm_all()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            row = db.execute("SELECT * FROM outgoing WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise SendError("send_request_not_found", 404)
            return self.view(row)

    def claim(self, body):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            bridge = self.gate(db)
            if (
                str(body.enrollment_epoch) != bridge["enrollment_epoch"]
                or body.database_id != bridge["database_id"]
                or str(body.account_ref) != bridge["account_ref"]
            ):
                raise SendError("send_enrollment_mismatch", 423)
            db.execute(
                "UPDATE outgoing SET status='failed',reason='enrollment_changed',text=NULL,"
                "reply_preview=NULL,location_json=NULL "
                "WHERE status='queued' AND (device_id!=? OR epoch!=? OR database_id!=? "
                "OR account_ref!=?)",
                (
                    self.config.device_id,
                    str(body.enrollment_epoch),
                    body.database_id,
                    str(body.account_ref),
                ),
            )
            self._drop_finished_blobs(db)
            row = db.execute(
                "SELECT * FROM outgoing WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return {"item": None}
            files = db.execute(
                "SELECT ordinal,name,mime,kind,sha256,size FROM outgoing_blobs "
                "WHERE request_id=? ORDER BY ordinal",
                (row["request_id"],),
            ).fetchall()
            # Commit before contacting Android. Crashes/lost responses never requeue an attempt.
            db.execute(
                "UPDATE outgoing SET status='dispatching',text=NULL,reply_preview=NULL,"
                "location_json=NULL,updated_at=? WHERE request_id=?",
                (time.time(), row["request_id"]),
            )
            item = {
                key: row[key]
                for key in (
                    "request_id",
                    "chat_id",
                    "text",
                    "epoch",
                    "database_id",
                    "account_ref",
                    "kind",
                    "thread_id",
                    "reply_to",
                    "reply_preview",
                    "emoticon_index",
                )
            }
            item["attachments"] = [dict(file) for file in files]
            item["location"] = json.loads(row["location_json"]) if row["location_json"] else None
            return {"item": item}

    def blob(self, request_id, ordinal):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT status FROM outgoing WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row or row["status"] != "dispatching":
                raise SendError("send_request_not_found", 404)
            blob = db.execute(
                "SELECT bytes FROM outgoing_blobs WHERE request_id=? AND ordinal=?",
                (request_id, ordinal),
            ).fetchone()
            if blob is None:
                raise SendError("send_request_not_found", 404)
            return blob["bytes"]

    def complete(self, request_id, result):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outgoing WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise SendError("send_request_not_found", 404)
            if row["status"] not in {"dispatching", "unknown", result.status}:
                raise SendError("invalid_send_transition")
            if result.status == "failed":
                reason = result.reason if result.reason in FAIL_REASONS else "device_rejected"
            else:
                reason = "delivery_not_confirmed"
            db.execute(
                "UPDATE outgoing SET status=?, reason=?, text=NULL, reply_preview=NULL, "
                "location_json=NULL, match_after_seq=?, updated_at=? WHERE request_id=?",
                (
                    result.status,
                    reason,
                    result.match_after_seq if result.status == "submitted" else None,
                    time.time(),
                    request_id,
                ),
            )
            self._drop_finished_blobs(db)
        return self.status(request_id)
