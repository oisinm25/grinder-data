import datetime as dt

import httpx
import pytest

from pipeline.notify import NotifyError, alert, alert_text, main, send

MON = dt.date(2026, 10, 26)
TOPIC = "pedestal-" + "secret-topic-1234567890"
GH = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "oisinm25/grinder-data", "GITHUB_RUN_ID": "42"}


class FakeNtfy:
    def __init__(self, statuses=(200,)):
        self.statuses, self.requests = list(statuses), []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        return httpx.Response(self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])


def test_alert_names_only_the_steps():
    assert alert_text(MON, ["publish"]) == "Pedestal Mon 26 Oct: publish failed."
    url = "https://github.com/oisinm25/grinder-data/actions/runs/42"
    assert alert_text(MON, ["ingest", "review"], url) == f"Pedestal Mon 26 Oct: ingest, review failed. {url}"


def test_alert_is_one_line_with_no_training_data():
    fake = FakeNtfy()
    sent = alert(MON, ["publish"], env={"NTFY_TOPIC": TOPIC, **GH}, transport=httpx.MockTransport(fake))
    (req,) = fake.requests
    body = req.content.decode()
    assert body == sent == "Pedestal Mon 26 Oct: publish failed. https://github.com/oisinm25/grinder-data/actions/runs/42"
    assert "\n" not in body
    for training in ("kg", "RPE", "HRV", "W ", "bench", "readiness"):
        assert training not in body
    assert req.url.path == f"/{TOPIC}" and req.headers["Click"].endswith("/actions/runs/42")
    assert req.headers["Title"] == "Pedestal pipeline" and req.headers["Tags"] == "warning"


def test_alerts_off_without_a_topic():
    fake = FakeNtfy()
    assert alert(MON, ["publish"], env={}, transport=httpx.MockTransport(fake)) is None
    assert fake.requests == []


def test_send_refuses_more_than_one_line():
    with pytest.raises(ValueError):
        send(TOPIC, "line 1\nline 2", transport=httpx.MockTransport(FakeNtfy()))


def test_send_retries_server_errors():
    waits, fake = [], FakeNtfy([503, 200])
    send(TOPIC, "x", transport=httpx.MockTransport(fake), sleep=waits.append)
    assert len(fake.requests) == 2 and waits == [1]


def test_errors_never_show_the_topic():
    with pytest.raises(NotifyError) as exc:
        send(TOPIC, "x", transport=httpx.MockTransport(FakeNtfy([403])))
    assert "403" in str(exc.value) and TOPIC not in str(exc.value)

    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(NotifyError) as exc:
        send(TOPIC, "x", transport=httpx.MockTransport(down), sleep=lambda s: None)
    assert TOPIC not in str(exc.value)


def test_custom_server():
    fake = FakeNtfy()
    alert(MON, ["x"], env={"NTFY_TOPIC": TOPIC, "NTFY_SERVER": "https://ntfy.example.org"}, transport=httpx.MockTransport(fake))
    assert fake.requests[0].url.host == "ntfy.example.org"


def test_cli_test_alert(capsys):
    fake = FakeNtfy()
    assert main(["--test"], env={"NTFY_TOPIC": TOPIC}, transport=httpx.MockTransport(fake)) == 0
    assert "sent a test alert" in capsys.readouterr().out and TOPIC not in fake.requests[0].content.decode()
    assert main(["--test"], env={}) == 2
    assert "NTFY_TOPIC is not set" in capsys.readouterr().err
