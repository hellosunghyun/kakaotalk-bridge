import base64
import hashlib
import hmac
import json
import socket

import pytest

from dot_plugin import network, webhooks
from dot_plugin.collector import QueryError

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
EPOCH = "epoch-1"
REF = "dev:epoch:111"
OTHER = "dev:epoch:222"


def target(**overrides):
    raw = {"id": "home", "url": "https://hooks.example.com/kakao", "secret": SECRET, **overrides}
    return webhooks.parse_targets([raw])[0]


def row(mid, body="hello", ref=REF, mine=False):
    return {
        "id": mid,
        "body": body,
        "source": "iris_db",
        "conversation_ref": ref,
        "database_ref": {"is_mine": mine},
    }


class FakeCollector:
    def __init__(self, rows, cursor=0, pruned=0, epoch=EPOCH):
        self.rows, self.cursor, self.pruned, self.epoch = rows, cursor, pruned, epoch
        self.gone = set()

    def checkpoint(self):
        return {
            "cursor": self.cursor,
            "cursor_epoch": self.epoch,
            "pruned_through_cursor": self.pruned,
        }

    def messages(self, after, limit=50):
        items = [r for r in self.rows if r["id"] > after][:limit]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else after,
            "has_more": False,
            "coverage": {"cursor_epoch": self.epoch},
        }

    def add(self, *rows):
        self.rows.extend(rows)
        self.cursor = max(r["id"] for r in self.rows)

    def get(self, path, **params):
        assert path == "/v2/context" and params["before"] == 0 and params["after"] == 0
        mid = params["message_id"]
        if mid in self.gone:
            raise QueryError("invalid_query_or_expired_cursor")
        r = next(r for r in self.rows if r["id"] == mid)
        return {
            "items": [
                {
                    "message_id": str(mid),
                    "conversation": {
                        "ref": r["conversation_ref"],
                        "name": "AI",
                        "kind": "MultiChat",
                    },
                    "sender": {"ref": "dev:epoch:user:9", "name": "혜림"},
                    "is_mine": r["database_ref"]["is_mine"],
                    "sent_at": "2026-10-25T16:07:19Z",
                    "collected_at": "2026-10-25T16:07:21Z",
                    "body": r["body"],
                    "message_type": "1",
                    "truncated": False,
                }
            ]
        }


class Recorder:
    def __init__(self, statuses=None):
        self.calls, self.statuses = [], list(statuses or [])

    def __call__(self, tgt, body, headers):
        self.calls.append((tgt.id, json.loads(body), headers, body))
        return self.statuses.pop(0) if self.statuses else 200


def forwarder(tmp_path, collector, targets, sender, clock=None):
    return webhooks.Forwarder(
        targets,
        collector,
        str(tmp_path / "webhooks.db"),
        sender=sender,
        clock=clock or (lambda: 1000.0),
    )


def test_disabled_by_default(tmp_path):
    assert (
        webhooks.load_targets({"KAKAO_WEBHOOK_TARGETS_FILE": str(tmp_path / "missing.json")}) == []
    )
    assert (
        webhooks.Forwarder.from_env(FakeCollector([]), {"KAKAO_WEBHOOK_TARGETS_FILE": ""}) is None
    )


def test_loads_file_and_inline(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"id": "a", "url": "https://x.example/h", "secret": SECRET}]})
    )
    assert [t.id for t in webhooks.load_targets({"KAKAO_WEBHOOK_TARGETS_FILE": str(path)})] == ["a"]
    inline = json.dumps(
        [{"id": "b", "url": "https://y.example/h", "secret": SECRET, "mentions": "@김성현"}]
    )
    (t,) = webhooks.load_targets(
        {"KAKAO_WEBHOOK_TARGETS": inline, "KAKAO_WEBHOOK_TARGETS_FILE": str(path)}
    )
    assert t.id == "b" and t.mentions == ("@김성현",)


def test_secret_file(tmp_path):
    (tmp_path / "s").write_text(SECRET + "\n")
    (t,) = webhooks.parse_targets(
        [{"id": "a", "url": "https://x.example/h", "secret_file": str(tmp_path / "s")}]
    )
    assert t.secret == SECRET


@pytest.mark.parametrize(
    "item, reason",
    [
        ({"url": "https://x.example/h", "secret": SECRET}, "invalid_or_duplicate_id"),
        ({"id": "a", "url": "https://x.example/h", "secret": "plain"}, "secret_must_be"),
        ({"id": "a", "url": "http://x.example/h", "secret": SECRET}, "http_requires_allow_tailnet"),
        ({"id": "a", "url": "ftp://x.example/h", "secret": SECRET}, "invalid_url"),
        ({"id": "a", "url": "https://u:p@x.example/h", "secret": SECRET}, "invalid_url"),
        ({"id": "a", "url": "https://x.example/h", "secret": SECRET, "bogus": 1}, "unknown_fields"),
        (
            {"id": "a", "url": "https://x.example/h", "secret": SECRET, "include_mine": "yes"},
            "include_mine",
        ),
    ],
)
def test_invalid_config_rejected_without_secret_echo(item, reason):
    with pytest.raises(webhooks.ConfigError) as error:
        webhooks.parse_targets([item])
    assert reason in str(error.value) and SECRET not in str(error.value)


def test_invalid_config_disables_instead_of_crashing(capsys):
    assert webhooks.Forwarder.from_env(FakeCollector([]), {"KAKAO_WEBHOOK_TARGETS": "[{"}) is None
    assert "disabled_invalid_config" in capsys.readouterr().out


def test_starts_at_current_cursor_without_backfill(tmp_path):
    collector = FakeCollector([row(1), row(2)], cursor=2)
    sender = Recorder()
    fw = forwarder(tmp_path, collector, [target()], sender)
    fw.tick()
    assert sender.calls == []
    collector.add(row(3, "new"))
    fw.tick()
    assert [c[1]["message_id"] for c in sender.calls] == ["3"]


def test_payload_and_standard_webhooks_signature(tmp_path):
    collector = FakeCollector([], cursor=0)
    sender = Recorder()
    fw = forwarder(tmp_path, collector, [target(mentions=["@김성현"])], sender)
    fw.tick()
    collector.add(row(5, "@김성현 이거 봐줘"), row(6, "그냥 잡담"))
    fw.tick()
    assert len(sender.calls) == 1
    _, payload, headers, body = sender.calls[0]
    assert payload == {
        "type": "message.created",
        "id": webhooks.event_id(EPOCH, "home", 5),
        "target_id": "home",
        "cursor_epoch": EPOCH,
        "message_id": "5",
        "sent_at": "2026-10-25T16:07:19Z",
        "collected_at": "2026-10-25T16:07:21Z",
        "conversation": {"ref": REF, "name": "AI", "kind": "MultiChat"},
        "sender": {"ref": "dev:epoch:user:9", "name": "혜림"},
        "is_mine": False,
        "body": "@김성현 이거 봐줘",
        "message_type": "1",
        "truncated": False,
        "mentions": {"keywords": ["@김성현"], "matched": True},
    }
    assert headers["webhook-id"] == payload["id"]
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == "v1," + expected
    assert headers["X-Kakao-Webhook-Target"] == "home" and "X-MCP-Subscription-Id" not in headers


def test_filters_conversation_and_mine(tmp_path):
    collector = FakeCollector([], cursor=0)
    sender = Recorder()
    targets = [
        target(id="only-ai", conversation_refs=[REF]),
        target(id="mine-too", include_mine=True),
    ]
    fw = forwarder(tmp_path, collector, targets, sender)
    fw.tick()
    collector.add(row(1, ref=REF), row(2, ref=OTHER), row(3, ref=REF, mine=True))
    fw.tick()
    got = sorted((c[0], c[1]["message_id"]) for c in sender.calls)
    assert got == [("mine-too", "1"), ("mine-too", "2"), ("mine-too", "3"), ("only-ai", "1")]


def test_retry_with_backoff_then_success_same_id(tmp_path):
    now = [1000.0]
    collector = FakeCollector([], cursor=0)
    sender = Recorder(statuses=[503, 200])
    fw = forwarder(tmp_path, collector, [target()], sender, clock=lambda: now[0])
    fw.tick()
    collector.add(row(1))
    fw.tick()
    fw.tick()  # backoff not elapsed
    assert len(sender.calls) == 1
    now[0] += 11
    fw.tick()
    assert len(sender.calls) == 2
    assert sender.calls[0][2]["webhook-id"] == sender.calls[1][2]["webhook-id"]
    now[0] += 10000
    fw.tick()
    assert len(sender.calls) == 2


def test_transport_failure_never_raises_and_gives_up(tmp_path):
    now = [1000.0]
    collector = FakeCollector([], cursor=0)

    def broken(*_):
        raise network.DeliveryError("transport_failed")

    fw = forwarder(tmp_path, collector, [target()], broken, clock=lambda: now[0])
    fw.tick()
    collector.add(row(1))
    for _ in range(webhooks.MAX_ATTEMPTS + 2):
        fw.tick()
        now[0] += 4000
    with fw.db() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_terminal_4xx_dropped(tmp_path):
    collector = FakeCollector([], cursor=0)
    sender = Recorder(statuses=[400])
    fw = forwarder(tmp_path, collector, [target()], sender)
    fw.tick()
    collector.add(row(1))
    fw.tick()
    fw.tick()
    assert len(sender.calls) == 1


def test_cursor_persists_across_restart(tmp_path):
    collector = FakeCollector([], cursor=0)
    first = Recorder(statuses=[500])
    fw = forwarder(tmp_path, collector, [target()], first)
    fw.tick()
    collector.add(row(1), row(2))
    fw.scan()  # queued, not yet delivered: simulates a crash before delivery
    second = Recorder()
    restarted = forwarder(tmp_path, collector, [target()], second, clock=lambda: 5000.0)
    restarted.tick()
    restarted.tick()
    assert sorted(c[1]["message_id"] for c in second.calls) == ["1", "2"]


def test_epoch_change_restarts_at_current_cursor(tmp_path):
    collector = FakeCollector([], cursor=0)
    sender = Recorder()
    fw = forwarder(tmp_path, collector, [target()], sender)
    fw.tick()
    collector.add(row(1), row(2))
    collector.epoch = "epoch-2"
    fw.tick()
    assert sender.calls == []


def test_unavailable_message_is_dropped(tmp_path):
    collector = FakeCollector([], cursor=0)
    sender = Recorder()
    fw = forwarder(tmp_path, collector, [target()], sender)
    fw.tick()
    collector.add(row(1))
    collector.gone.add(1)
    fw.tick()
    assert sender.calls == []


def test_removed_target_queue_is_discarded(tmp_path):
    collector = FakeCollector([], cursor=0)
    fw = forwarder(tmp_path, collector, [target(id="old")], Recorder(statuses=[500]))
    fw.tick()
    collector.add(row(1))
    fw.scan()
    sender = Recorder()
    fw2 = forwarder(tmp_path, collector, [target(id="new")], sender, clock=lambda: 9999.0)
    fw2.tick()
    with fw2.db() as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE target_id='old'").fetchone()[0] == 0
    assert sender.calls == []


def fake_dns(monkeypatch, *ips):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips],
    )


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "172.17.0.1"]
)
def test_private_addresses_blocked_even_with_tailnet_flag(monkeypatch, ip):
    fake_dns(monkeypatch, ip)
    for flag in (False, True):
        with pytest.raises(network.DeliveryError):
            webhooks.resolve("https://hooks.example.com/x", flag)


def test_tailnet_requires_flag_and_http_only_to_tailnet(monkeypatch):
    fake_dns(monkeypatch, "100.101.102.103")
    with pytest.raises(network.DeliveryError):
        webhooks.resolve("https://box.tail.ts.net/x", False)
    assert webhooks.resolve("https://box.tail.ts.net/x", True)[2] == ["100.101.102.103"]
    assert webhooks.resolve("http://100.101.102.103:8080/x", True)[1] == 8080
    fake_dns(monkeypatch, "93.184.216.34")
    with pytest.raises(network.DeliveryError):
        webhooks.resolve("http://public.example/x", True)
    assert webhooks.resolve("https://public.example/x", False)[2] == ["93.184.216.34"]


def test_post_pins_validated_address_and_does_not_follow_redirects(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    seen = {}

    class Conn:
        def __init__(self, host, port, address):
            seen.update(host=host, port=port, address=address)

        def request(self, method, path, body=None, headers=None):
            seen.update(method=method, path=path)

        def getresponse(self):
            class R:
                status = 302

                def read(self, n):
                    return b""

            return R()

        def close(self):
            pass

    monkeypatch.setattr(network, "PinnedHTTPS", Conn)
    status = webhooks.post(target(url="https://hooks.example.com/k?a=1"), b"{}", {})
    assert status == 302
    assert seen == {
        "host": "hooks.example.com",
        "port": 443,
        "address": "93.184.216.34",
        "method": "POST",
        "path": "/k?a=1",
    }
