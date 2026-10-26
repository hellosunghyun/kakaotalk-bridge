"""Static outbound webhooks: push newly collected messages to servers the owner configures.

Unlike the MCP ``message.created`` subscription (signal-only, OAuth, 24h TTL), this forwarder
is configured statically by the server owner and sends the message body. It is disabled
unless targets are configured.

Delivery is at-least-once. Each target has its own durable source cursor and outbox in a
small SQLite file; the outbox stores only message ids (bodies are re-read at send time), so
no message text is written to this file. Failures never block collection or other targets.
"""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dot_plugin import network
from dot_plugin.collector import QueryError
from dot_plugin.config import EVENT

DEFAULT_FILE = "/run/webhooks/targets.json"
MAX_ATTEMPTS = 10
MAX_QUEUE = 1000
# Instant acks: a short fixed reply posted through the normal send path (same as the MCP
# send_message tool) as soon as a matching message is collected. Late acks are pointless.
ACK_MAX_AGE = 120  # seconds after detection to keep retrying a send
ACK_MAX_MESSAGE_AGE = 600  # never ack messages older than this (e.g. after downtime)
ACK_RETRYABLE = {
    "send_rate_limited",
    "sending_unavailable_until_kakaotalk_notification",
}
ACK_NAMESPACE = uuid.UUID("6f1d2a7e-3c4b-5d6e-8f90-a1b2c3d4e5f6")
# Tailscale CGNAT range and ULA prefix; only reachable with allow_tailnet.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class ConfigError(ValueError):
    pass


def log(**fields):
    # Never log URLs, secrets or message bodies.
    print(json.dumps({"component": "webhooks", **fields}, ensure_ascii=False), flush=True)


@dataclass(frozen=True)
class Target:
    id: str
    url: str
    secret: str
    conversation_refs: tuple = ()
    mentions: tuple = ()
    include_mine: bool = False
    allow_tailnet: bool = False

    def matches(self, row):
        """Return (deliver, matched_keywords) for a /v1/messages row."""
        if row.get("source") != "iris_db":
            return False, []
        if self.conversation_refs and row.get("conversation_ref") not in self.conversation_refs:
            return False, []
        if not self.include_mine and row.get("database_ref", {}).get("is_mine"):
            return False, []
        hits = mention_hits(row.get("body") or "", self.mentions)
        if self.mentions and not hits:
            return False, []
        return True, hits


@dataclass(frozen=True)
class AckRule:
    """Post ``text`` into the same room when a message there mentions one of ``mentions``."""

    id: str
    text: str
    conversation_refs: tuple
    mentions: tuple
    include_mine: bool = True
    skip_prefixes: tuple = ()

    def matches(self, row):
        if row.get("source") != "iris_db":
            return False
        if row.get("conversation_ref") not in self.conversation_refs:
            return False
        if not self.include_mine and row.get("database_ref", {}).get("is_mine"):
            return False
        body = row.get("body") or ""
        stripped = body.lstrip()
        # Self-loop guard: never answer our own ack or any other bot reply prefix.
        if any(stripped.startswith(p) for p in (self.text, *self.skip_prefixes)):
            return False
        return bool(mention_hits(body, self.mentions))


def ack_request_id(epoch, rule_id, message_id):
    """Deterministic UUID so a retry (even after a restart) reuses the same send request."""
    return str(uuid.uuid5(ACK_NAMESPACE, f"{epoch}:{rule_id}:{message_id}"))


def parse_acks(raw):
    """Validate the optional ``instant_acks`` list. Raises ConfigError."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 5:
        raise ConfigError("instant_acks_must_be_a_list_of_at_most_5")
    allowed = {
        "id",
        "text",
        "conversation_refs",
        "mentions",
        "include_mine",
        "skip_prefixes",
        "enabled",
    }
    rules, seen = [], set()
    for index, item in enumerate(raw):
        where = f"instant_acks[{index}]"
        if not isinstance(item, dict) or set(item) - allowed:
            raise ConfigError(where + ":unknown_fields")
        if "enabled" in item and type(item["enabled"]) is not bool:
            raise ConfigError(where + ":enabled_must_be_boolean")
        if item.get("enabled", True) is False:
            continue
        rid = item.get("id")
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) or rid in seen:
            raise ConfigError(where + ":invalid_or_duplicate_id")
        seen.add(rid)
        text = item.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
            raise ConfigError(where + ":invalid_text")
        if "include_mine" in item and type(item["include_mine"]) is not bool:
            raise ConfigError(where + ":include_mine_must_be_boolean")
        for name, required in (
            ("conversation_refs", True),
            ("mentions", True),
            ("skip_prefixes", False),
        ):
            values = item.get(name, [])
            if (
                not isinstance(values, list)
                or len(values) > 20
                or (required and not values)
                or not all(isinstance(v, str) and 1 <= len(v) <= 256 for v in values)
            ):
                raise ConfigError(where + f":invalid_{name}")
        rules.append(
            AckRule(
                rid,
                text.strip(),
                tuple(item["conversation_refs"]),
                tuple(item["mentions"]),
                item.get("include_mine", True),
                tuple(item.get("skip_prefixes", [])),
            )
        )
    return rules


def mention_hits(body, keywords):
    lowered = body.casefold()
    return [k for k in keywords if k.casefold() in lowered]


def _check_url(url, allow_tailnet):
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (
        parsed is None
        or parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(url) > 4096
    ):
        raise ConfigError("invalid_url")
    if parsed.scheme == "http" and not allow_tailnet:
        raise ConfigError("http_requires_allow_tailnet")


def parse_targets(raw):
    """Validate the JSON target list. Raises ConfigError without echoing secrets."""
    if isinstance(raw, dict):
        raw = raw.get("targets")
    if not isinstance(raw, list) or len(raw) > 20:
        raise ConfigError("targets_must_be_a_list_of_at_most_20")
    targets, seen = [], set()
    allowed = {
        "id",
        "url",
        "secret",
        "secret_file",
        "conversation_refs",
        "mentions",
        "include_mine",
        "allow_tailnet",
        "enabled",
    }
    for index, item in enumerate(raw):
        where = f"target[{index}]"
        if not isinstance(item, dict) or set(item) - allowed:
            raise ConfigError(where + ":unknown_fields")
        if item.get("enabled", True) is False:
            continue
        tid = item.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid) or tid in seen:
            raise ConfigError(where + ":invalid_or_duplicate_id")
        seen.add(tid)
        secret = item.get("secret")
        if secret is None and isinstance(item.get("secret_file"), str):
            try:
                secret = Path(item["secret_file"]).read_text().strip()
            except OSError:
                raise ConfigError(where + ":secret_file_unreadable") from None
        try:
            network.signing_key(secret)
        except ValueError:
            raise ConfigError(where + ":secret_must_be_whsec_base64_24_to_64_bytes") from None
        for flag in ("include_mine", "allow_tailnet"):
            if flag in item and type(item[flag]) is not bool:
                raise ConfigError(where + f":{flag}_must_be_boolean")
        refs = item.get("conversation_refs", [])
        mentions = item.get("mentions", [])
        if isinstance(mentions, str):
            mentions = [mentions]
        for name, values in (("conversation_refs", refs), ("mentions", mentions)):
            if (
                not isinstance(values, list)
                or len(values) > 100
                or not all(isinstance(v, str) and 1 <= len(v) <= 256 for v in values)
            ):
                raise ConfigError(where + f":invalid_{name}")
        _check_url(item.get("url"), item.get("allow_tailnet", False))
        targets.append(
            Target(
                tid,
                item["url"],
                secret,
                tuple(refs),
                tuple(mentions),
                item.get("include_mine", False),
                item.get("allow_tailnet", False),
            )
        )
    return targets


def load_targets(environ=None):
    return load_config(environ)[0]


def load_config(environ=None):
    """Return (targets, instant_acks). Acks are only read from a JSON object config."""
    environ = os.environ if environ is None else environ
    inline = environ.get("KAKAO_WEBHOOK_TARGETS", "").strip()
    path = environ.get("KAKAO_WEBHOOK_TARGETS_FILE", DEFAULT_FILE)
    if inline:
        text = inline
    elif path and Path(path).is_file():
        try:
            text = Path(path).read_text()
        except OSError:
            raise ConfigError("targets_file_unreadable") from None
        if not text.strip():
            return [], []
    else:
        return [], []
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("targets_not_valid_json") from None
    acks = parse_acks(data.get("instant_acks")) if isinstance(data, dict) else []
    if isinstance(data, dict) and "targets" not in data:
        data = {"targets": []}
    targets = parse_targets(data)
    if str(environ.get("KAKAO_INSTANT_ACKS", "")).strip().lower() in {"0", "off", "false", "no"}:
        acks = []
    return targets, acks


# --- transport ---------------------------------------------------------------


def resolve(url, allow_tailnet):
    target = urlsplit(url)
    _check_url(url, allow_tailnet)
    port = target.port or (443 if target.scheme == "https" else 80)
    try:
        entries = socket.getaddrinfo(target.hostname, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(item[4][0] for item in entries))
        if not addresses:
            raise ValueError
        tailnet_only = True
        for address in addresses:
            ip = ipaddress.ip_address(address)
            in_tailnet = any(ip in net for net in TAILNET)
            tailnet_only &= in_tailnet
            if allow_tailnet and in_tailnet:
                continue
            if (
                not ip.is_global
                or ip.is_multicast
                or ip.is_unspecified
                or getattr(ip, "scope_id", None)
            ):
                raise ValueError
            if isinstance(ip, ipaddress.IPv6Address) and (
                ip.ipv4_mapped or ip.sixtofour or ip.teredo
            ):
                raise ValueError
        # Plain HTTP only ever goes to tailnet addresses (WireGuard-encrypted).
        if target.scheme == "http" and not tailnet_only:
            raise ValueError
    except (OSError, ValueError):
        raise network.DeliveryError("invalid_url") from None
    ordered = sorted(addresses, key=lambda item: ipaddress.ip_address(item).version)
    return target, port, ordered


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port, timeout=10)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


def post(target_cfg, body, headers):
    target, port, addresses = resolve(target_cfg.url, target_cfg.allow_tailnet)
    cls = network.PinnedHTTPS if target.scheme == "https" else PinnedHTTP
    conn = cls(target.hostname, port, addresses[0])
    try:
        path = (target.path or "/") + ("?" + target.query if target.query else "")
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        response.read(65537)
        return response.status  # Redirects count as failures and are never followed.
    except TimeoutError:
        raise network.DeliveryError("timeout") from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise network.DeliveryError("transport_failed") from None
    finally:
        conn.close()


def sign(target, event_id, body, now=None):
    headers = network.signed_headers(
        {"id": target.id, "secret": target.secret}, event_id, body, now
    )
    headers.pop("X-MCP-Subscription-Id", None)
    headers["X-Kakao-Webhook-Target"] = target.id
    headers["User-Agent"] = "kakaotalk-bridge-webhooks/1"
    return headers


def iso_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def event_id(epoch, target_id, message_id):
    return "msg_" + hashlib.sha256(f"{epoch}:{target_id}:{message_id}".encode()).hexdigest()[:40]


def build_payload(target, epoch, row, message, keywords):
    conversation = message.get("conversation") or {}
    sender = message.get("sender") or {}
    return {
        "type": EVENT,
        "id": event_id(epoch, target.id, row["id"]),
        "target_id": target.id,
        "cursor_epoch": epoch,
        "message_id": str(row["id"]),
        "sent_at": message.get("sent_at"),
        "collected_at": message.get("collected_at"),
        "conversation": {
            "ref": conversation.get("ref") or row.get("conversation_ref"),
            "name": conversation.get("name"),
            "kind": conversation.get("kind"),
        },
        "sender": {"ref": sender.get("ref"), "name": sender.get("name")},
        "is_mine": bool(message.get("is_mine")),
        "body": message.get("body"),
        "message_type": message.get("message_type"),
        "truncated": bool(message.get("truncated")),
        "mentions": {"keywords": list(keywords), "matched": bool(keywords)},
    }


# --- forwarder ---------------------------------------------------------------


class Forwarder:
    def __init__(self, targets, collector, database, sender=post, clock=time.time, acks=()):
        self.targets = {t.id: t for t in targets}
        self.acks = {a.id: a for a in acks}
        self.collector, self.database = collector, database
        self.sender, self.clock = sender, clock
        self.stop = threading.Event()
        self.lock = threading.RLock()
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS cursors (target_id TEXT PRIMARY KEY, epoch TEXT NOT NULL, cursor INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbox (target_id TEXT, message_id INTEGER, epoch TEXT NOT NULL, "
                "keywords TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, "
                "PRIMARY KEY(target_id, message_id))"
            )
            # Ack ledger: one row per (rule, message) is the dedupe across ticks and restarts.
            # Stores ids, the room ref and timestamps only; never message text.
            db.execute(
                "CREATE TABLE IF NOT EXISTS acks (rule_id TEXT, message_id INTEGER, conversation_ref TEXT NOT NULL, "
                "request_id TEXT NOT NULL, created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt REAL NOT NULL, state TEXT NOT NULL, PRIMARY KEY(rule_id, message_id))"
            )
        try:
            os.chmod(database, 0o600)
        except OSError:
            pass

    @classmethod
    def from_env(cls, collector, environ=None):
        environ = os.environ if environ is None else environ
        try:
            targets, acks = load_config(environ)
        except ConfigError as error:
            log(state="disabled_invalid_config", reason=str(error))
            return None
        if not targets and not acks:
            return None
        database = environ.get("DOT_WEBHOOK_DB", "/data/webhooks.db")
        try:
            forwarder = cls(targets, collector, database, acks=acks)
        except (OSError, sqlite3.Error):
            log(state="disabled_state_unavailable")
            return None
        log(state="enabled", targets=[t.id for t in targets], instant_acks=[a.id for a in acks])
        return forwarder

    def db(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        return _Closing(conn, self.lock)

    def tick(self):
        if self.acks:
            # Acks first: they are the latency-sensitive path; a slow webhook must not delay them.
            try:
                self.scan_acks()
                self.send_acks()
            except Exception:  # noqa: BLE001 — never let acks block webhook forwarding
                log(state="instant_ack_unavailable")
        self.scan()
        self.deliver_due()

    # --- instant acks ---------------------------------------------------------

    def scan_acks(self):
        checkpoint = self.collector.checkpoint()
        epoch = checkpoint["cursor_epoch"]
        for rule in self.acks.values():
            key = "ack:" + rule.id
            with self.db() as db:
                row = db.execute(
                    "SELECT epoch, cursor FROM cursors WHERE target_id=?", (key,)
                ).fetchone()
                if row is None or row["epoch"] != epoch:
                    # Never backfill: start at the current cursor.
                    db.execute(
                        "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(target_id) DO UPDATE SET epoch=excluded.epoch, cursor=excluded.cursor",
                        (key, epoch, checkpoint["cursor"]),
                    )
                    log(instant_ack=rule.id, state="cursor_started", cursor=checkpoint["cursor"])
                    continue
                cursor = max(row["cursor"], checkpoint["pruned_through_cursor"])
            if cursor >= checkpoint["cursor"]:
                continue
            page = self.collector.messages(cursor, 100)
            if page.get("coverage", {}).get("cursor_epoch", epoch) != epoch:
                continue
            now = self.clock()
            with self.db() as db:
                for item in page["items"]:
                    if not rule.matches(item):
                        continue
                    source_time = item.get("source_time")
                    if (
                        isinstance(source_time, (int, float))
                        and now - source_time / 1000 > ACK_MAX_MESSAGE_AGE
                    ):
                        log(instant_ack=rule.id, message_id=item["id"], state="skipped_stale")
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO acks(rule_id,message_id,conversation_ref,request_id,created_at,attempts,next_attempt,state) "
                        "VALUES(?,?,?,?,?,0,?,'pending')",
                        (
                            rule.id,
                            item["id"],
                            item["conversation_ref"],
                            ack_request_id(epoch, rule.id, item["id"]),
                            now,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE cursors SET cursor=? WHERE target_id=? AND epoch=?",
                    (page["next_cursor"], key, epoch),
                )

    def send_acks(self, limit=5):
        now = self.clock()
        with self.db() as db:
            db.execute("DELETE FROM acks WHERE created_at<?", (now - 7 * 86400,))
            due = db.execute(
                "SELECT * FROM acks WHERE state='pending' AND next_attempt<=? ORDER BY message_id LIMIT ?",
                (now, limit),
            ).fetchall()
        for item in due:
            rule = self.acks.get(item["rule_id"])
            fields = {"instant_ack": item["rule_id"], "message_id": item["message_id"]}
            if rule is None or now - item["created_at"] > ACK_MAX_AGE:
                self._ack_state(item, "expired", item["attempts"], now)
                log(**fields, state="expired")
                continue
            attempts = item["attempts"] + 1
            try:
                result = self.collector.outgoing(
                    body={
                        "request_id": item["request_id"],
                        "conversation_ref": item["conversation_ref"],
                        "text": rule.text,
                    }
                )
            except QueryError as exc:
                reason = str(exc)
                state = "pending" if reason in ACK_RETRYABLE else "failed"
                self._ack_state(item, state, attempts, now + 2)
                log(**fields, state="retry" if state == "pending" else "failed", reason=reason)
                continue
            except Exception:  # noqa: BLE001 — result unknown: retry with the same request id
                self._ack_state(item, "pending", attempts, now + 2)
                log(**fields, state="retry", reason="send_result_unavailable")
                continue
            self._ack_state(item, "sent", attempts, now)
            status = result.get("status") if isinstance(result, dict) else None
            log(
                **fields,
                state="sent",
                send_status=status if isinstance(status, str) else None,
                latency_ms=int((self.clock() - item["created_at"]) * 1000),
            )

    def _ack_state(self, item, state, attempts, next_attempt):
        with self.db() as db:
            db.execute(
                "UPDATE acks SET state=?, attempts=?, next_attempt=? WHERE rule_id=? AND message_id=?",
                (state, attempts, next_attempt, item["rule_id"], item["message_id"]),
            )

    def scan(self):
        checkpoint = self.collector.checkpoint()
        epoch = checkpoint["cursor_epoch"]
        for target in self.targets.values():
            with self.db() as db:
                row = db.execute(
                    "SELECT epoch, cursor FROM cursors WHERE target_id=?", (target.id,)
                ).fetchone()
                if row is None or row["epoch"] != epoch:
                    # New target or restored database: start at the current cursor, no backfill.
                    db.execute(
                        "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(target_id) DO UPDATE SET epoch=excluded.epoch, cursor=excluded.cursor",
                        (target.id, epoch, checkpoint["cursor"]),
                    )
                    db.execute(
                        "DELETE FROM outbox WHERE target_id=? AND epoch<>?", (target.id, epoch)
                    )
                    log(target=target.id, state="cursor_started", cursor=checkpoint["cursor"])
                    continue
                cursor = max(row["cursor"], checkpoint["pruned_through_cursor"])
                queued = db.execute(
                    "SELECT count(*) FROM outbox WHERE target_id=?", (target.id,)
                ).fetchone()[0]
            if queued >= MAX_QUEUE or cursor >= checkpoint["cursor"]:
                continue  # Backpressure keeps the source cursor in place.
            page = self.collector.messages(cursor, 100)
            if page.get("coverage", {}).get("cursor_epoch", epoch) != epoch:
                continue
            now = self.clock()
            with self.db() as db:
                for item in page["items"]:
                    ok, keywords = target.matches(item)
                    if ok:
                        db.execute(
                            "INSERT OR IGNORE INTO outbox(target_id,message_id,epoch,keywords,attempts,next_attempt) VALUES(?,?,?,?,0,?)",
                            (
                                target.id,
                                item["id"],
                                epoch,
                                json.dumps(keywords, ensure_ascii=False),
                                now,
                            ),
                        )
                db.execute(
                    "UPDATE cursors SET cursor=? WHERE target_id=? AND epoch=?",
                    (page["next_cursor"], target.id, epoch),
                )

    def deliver_due(self, limit=20):
        with self.db() as db:
            due = db.execute(
                "SELECT * FROM outbox WHERE next_attempt<=? ORDER BY next_attempt, message_id LIMIT ?",
                (self.clock(), limit),
            ).fetchall()
        failed_targets = set()
        for item in due:
            if self.stop.is_set():
                break
            target = self.targets.get(item["target_id"])
            if target is None:
                with self.db() as db:
                    db.execute(
                        "DELETE FROM outbox WHERE target_id=? AND message_id=?",
                        (item["target_id"], item["message_id"]),
                    )
                continue
            if target.id in failed_targets:
                continue  # Keep order: do not skip ahead of a failing message this tick.
            self._deliver_one(target, item, failed_targets)

    def _deliver_one(self, target, item, failed_targets):
        key = (item["target_id"], item["message_id"])
        try:
            context = self.collector.get(
                "/v2/context", message_id=item["message_id"], before=0, after=0
            )
            messages = [
                m
                for m in context.get("items", [])
                if str(m.get("message_id")) == str(item["message_id"])
            ]
        except QueryError:
            messages = []  # Pruned or no longer available: nothing to send.
        if not messages:
            with self.db() as db:
                db.execute("DELETE FROM outbox WHERE target_id=? AND message_id=?", key)
            log(target=target.id, delivery="dropped_message_unavailable")
            return
        row = {
            "id": item["message_id"],
            "conversation_ref": messages[0].get("conversation", {}).get("ref"),
        }
        payload = build_payload(
            target, item["epoch"], row, messages[0], json.loads(item["keywords"])
        )
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            status = self.sender(target, body, sign(target, payload["id"], body))
        except network.DeliveryError:
            status = 0
        attempts = item["attempts"] + 1
        success = 200 <= status < 300
        terminal = (
            success
            or status in (400, 410, 413, 422)
            or 300 <= status < 400
            or attempts >= MAX_ATTEMPTS
        )
        with self.db() as db:
            if terminal:
                db.execute("DELETE FROM outbox WHERE target_id=? AND message_id=?", key)
            else:
                db.execute(
                    "UPDATE outbox SET attempts=?, next_attempt=? WHERE target_id=? AND message_id=?",
                    (attempts, self.clock() + min(3600, 5 * 2**attempts), *key),
                )
        if not success:
            failed_targets.add(target.id)
        log(
            target=target.id,
            delivery="accepted" if success else "failed" if terminal else "retry",
            status=status,
            attempts=attempts,
        )

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — isolate failures; never log bodies or URLs
                log(state="source_or_delivery_unavailable")
            # Poll faster when instant acks are configured (local collector calls only).
            self.stop.wait(0.5 if self.acks else 2)


class _Closing:
    def __init__(self, conn, lock):
        self.conn, self.lock = conn, lock

    def __enter__(self):
        self.lock.acquire()
        return self.conn.__enter__()

    def __exit__(self, *exc):
        try:
            return self.conn.__exit__(*exc)
        finally:
            self.conn.close()
            self.lock.release()
