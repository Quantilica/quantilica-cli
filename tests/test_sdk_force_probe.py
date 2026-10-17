"""Testes: sync --force, check --out, probe Range e skip-unavailable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from quantilica.core.http import HttpClient
from quantilica.core.manifests import manifest_sidecar_path
from typer.testing import CliRunner

from quantilica.cli.sdk import CheckPlan, FetcherApp
from tests.test_sdk import _grouped, make_app, sample_list_datasets

runner = CliRunner()
ENTRY = sample_list_datasets("exp")[0]


# ---------------------------------------------------------------
# force
# ---------------------------------------------------------------


class _RecClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def head_last_modified_date(self, url: str) -> None:
        return None

    def download_with_manifest(self, url, target, **kwargs):
        self.calls.append(kwargs)
        return Path(target)


def test_download_file_force_only_when_true(tmp_path: Path):
    client = _RecClient()
    app = make_app(client=client)
    app._download_file("https://x.test/a.csv", tmp_path / "d" / "a.csv")
    app._download_file("https://x.test/a.csv", tmp_path / "d" / "a.csv", force=True)
    assert "force" not in client.calls[0]
    assert client.calls[1]["force"] is True


def test_download_entry_force_propagates(tmp_path: Path):
    client = _RecClient()
    app = make_app(client=client)
    app.download_entry(ENTRY, tmp_path)
    app.download_entry(ENTRY, tmp_path, force=True)
    assert "force" not in client.calls[0]
    assert client.calls[1]["force"] is True


def test_download_datasets_without_force_keeps_old_call(tmp_path: Path):
    app = make_app(client=HttpClient(attempts=1))
    seen: list[dict[str, Any]] = []

    def fake_entry(entry, output_dir, *, dry_run=False, progress=None):
        seen.append({"entry": entry})
        return output_dir

    app.download_entry = fake_entry  # type: ignore[method-assign]
    done, total, errors = app.download_datasets([ENTRY], tmp_path, workers=1)
    assert (done, total, errors) == (1, 1, [])
    assert len(seen) == 1


def test_download_datasets_force_passed(tmp_path: Path):
    app = make_app(client=HttpClient(attempts=1))
    seen: list[bool] = []

    def fake_entry(entry, output_dir, *, dry_run=False, progress=None, force=False):
        seen.append(force)
        return output_dir

    app.download_entry = fake_entry  # type: ignore[method-assign]
    app.download_datasets([ENTRY], tmp_path, workers=1, force=True)
    assert seen == [True]


def test_sync_force_flag_with_from_plan(tmp_path: Path, monkeypatch):
    app = make_app()
    plan = CheckPlan(fetcher="f", output_dir=str(tmp_path), generated_at="x")
    items = [
        _item("a", "download"),
        _item("b", "skip-up-to-date"),
        _item("c", "skip-unavailable"),
    ]
    plan = CheckPlan("f", str(tmp_path), "x", items)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.to_json(), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake(entries, out, workers=4, **kw):
        captured["ids"] = [e["id"] for e in entries]
        captured["kw"] = kw
        return len(entries), len(entries), []

    monkeypatch.setattr(app, "download_datasets", fake)
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "--from-plan", str(plan_file), "--force", "-o", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert captured["ids"] == ["a"]
    assert captured["kw"] == {"force": True}

    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "--from-plan", str(plan_file), "-o", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert captured["kw"] == {}


def _item(eid: str, action: str):
    from quantilica.cli.sdk import CheckPlanItem

    return CheckPlanItem(
        dataset="exp",
        id=eid,
        url="https://x.test/" + eid,
        partition=None,
        local_path="/tmp/x",
        remote_etag=None,
        remote_last_modified=None,
        remote_size=None,
        local_exists=False,
        action=action,
        reason="r",
        entry={"id": eid, "url": "https://x.test/" + eid},
    )


# ---------------------------------------------------------------
# check --out
# ---------------------------------------------------------------


def _head_ok(request):
    return httpx2.Response(200, headers={"Content-Length": "10"})


def test_check_out_writes_json_and_no_stdout_json(tmp_path: Path):
    app = make_app(
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(_head_ok))
    )
    out = tmp_path / "sub" / "plan.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "check",
            "exp",
            "-o",
            str(tmp_path / "data"),
            "--json",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["items"][0]["action"] == "download"
    assert '"items"' not in result.output
    assert "1 download" in result.output
    assert not list(out.parent.glob("*.tmp"))
    CheckPlan.from_json(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------
# probe Range
# ---------------------------------------------------------------


class _TrackedStream(httpx2.SyncByteStream):
    def __init__(self) -> None:
        self.read = False

    def __iter__(self):
        self.read = True
        yield b"x" * 100


def _probe_app(handler) -> FetcherApp:
    return make_app(
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(handler))
    )


def test_probe_206_content_range(tmp_path: Path):
    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(405)
        assert request.headers["Range"] == "bytes=0-0"
        return httpx2.Response(
            206,
            headers={
                "Content-Range": "bytes 0-0/12345",
                "ETag": '"e1"',
                "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            },
            content=b"x",
        )

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason == "range-probe"
    assert item.remote_size == 12345
    assert item.remote_etag == '"e1"'
    assert item.remote_last_modified == "Mon, 01 Jan 2024 00:00:00 GMT"


def test_probe_206_size_match_local_and_sidecar(tmp_path: Path):
    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(403)
        return httpx2.Response(206, headers={"Content-Range": "bytes 0-0/10"})

    app = _probe_app(handler)
    local = app.path_builder(tmp_path, ENTRY, None)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"0123456789")
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "skip-up-to-date"
    assert item.reason == "size-match-range-probe"

    local.unlink()
    manifest_sidecar_path(local).write_text(
        json.dumps({"size_bytes": 10}), encoding="utf-8"
    )
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "skip-up-to-date"

    local.write_bytes(b"short")
    manifest_sidecar_path(local).unlink()
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason == "range-probe"


def test_probe_server_ignores_range_body_not_read(tmp_path: Path):
    body = _TrackedStream()

    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(405)
        return httpx2.Response(200, headers={"Content-Length": "999"}, stream=body)

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.remote_size == 999
    assert item.reason == "range-probe"
    assert body.read is False


def test_probe_403_also_falls_back_to_metadata_unavailable(tmp_path: Path):
    def handler(request):
        return httpx2.Response(403)

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason.startswith("metadata-unavailable")


def test_probe_skipped_without_stream_api(tmp_path: Path):
    class NoStream:
        def head(self, url):
            from quantilica.core.http import HttpStatusError

            raise HttpStatusError(url, 405)

    item = make_app(client=NoStream()).check_entry(ENTRY, tmp_path)
    assert item.reason.startswith("metadata-unavailable")


# ---------------------------------------------------------------
# skip-unavailable
# ---------------------------------------------------------------


def test_all_404_is_skip_unavailable(tmp_path: Path):
    def handler(request):
        return httpx2.Response(404)

    entry = {**ENTRY, "fallback_urls": ["https://example.test/alt.csv"]}
    item = _probe_app(handler).check_entry(entry, tmp_path)
    assert item.action == "skip-unavailable"
    assert item.reason == "remote-404"


def test_404_then_other_error_is_not_skip_unavailable(tmp_path: Path):
    def handler(request):
        if request.url.path.endswith("alt.csv"):
            return httpx2.Response(500)
        return httpx2.Response(404)

    entry = {**ENTRY, "fallback_urls": ["https://example.test/alt.csv"]}
    item = _probe_app(handler).check_entry(entry, tmp_path)
    assert item.action == "download"


def test_from_dict_accepts_skip_unavailable_rejects_invalid():
    payload = CheckPlan("f", "/o", "t", [_item("a", "skip-unavailable")]).to_dict()
    assert CheckPlan.from_dict(payload).items[0].action == "skip-unavailable"
    payload["items"][0]["action"] = "bogus"
    with pytest.raises(ValueError, match="not a check plan"):
        CheckPlan.from_dict(payload)


# ---------------------------------------------------------------
# entry_from_plan
# ---------------------------------------------------------------


def _run_from_plan(app: FetcherApp, tmp_path: Path, monkeypatch) -> list[dict]:
    entry = {"id": "a", "url": "https://x.test/a", "when": {"__datetime__": True}}
    item = _item("a", "download")
    item = type(item)(**{**item.__dict__, "entry": entry})
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(CheckPlan("f", "/o", "t", [item]).to_json(), encoding="utf-8")
    captured: list[dict] = []

    def fake(entries, out, workers=4, **kw):
        captured.extend(entries)
        return len(entries), len(entries), []

    monkeypatch.setattr(app, "download_datasets", fake)
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--from-plan", str(plan_file)]
    )
    assert result.exit_code == 0, result.output
    return captured


def test_entry_from_plan_default_returns_shallow_copy(tmp_path: Path, monkeypatch):
    app = make_app()
    data = {"id": "a", "n": 1}
    out = app.entry_from_plan(data)
    assert out == data and out is not data
    got = _run_from_plan(app, tmp_path, monkeypatch)
    assert got[0]["when"] == {"__datetime__": True}


def test_entry_from_plan_subclass_override(tmp_path: Path, monkeypatch):
    class Sub(FetcherApp):
        def entry_from_plan(self, data):
            return {**data, "when": "restored"}

    base = make_app()
    app = Sub(
        name="comex-fetcher",
        groups_dict=base.groups,
        aliases_dict=base.aliases,
        list_datasets=sample_list_datasets,
    )
    got = _run_from_plan(app, tmp_path, monkeypatch)
    assert got[0]["when"] == "restored"


def test_entry_from_plan_instance_assignment(tmp_path: Path, monkeypatch):
    app = make_app()
    app.entry_from_plan = lambda data: {**data, "when": "restored"}  # type: ignore[method-assign]
    got = _run_from_plan(app, tmp_path, monkeypatch)
    assert got[0]["when"] == "restored"


def test_default_client_max_resumes_env(monkeypatch):
    """QUANTILICA_HTTP_MAX_RESUMES só é repassado quando definido."""
    from quantilica.core.http import HttpClient

    from quantilica.cli.sdk import default_client

    monkeypatch.delenv("QUANTILICA_HTTP_MAX_RESUMES", raising=False)
    default = HttpClient().max_resumes
    assert default_client().max_resumes == default

    monkeypatch.setenv("QUANTILICA_HTTP_MAX_RESUMES", "7")
    assert default_client().max_resumes == 7


def test_sync_force_without_from_plan(tmp_path: Path, monkeypatch):
    app = make_app()
    captured: dict[str, Any] = {}

    def fake(entries, out, workers=4, **kw):
        captured["n"] = len(entries)
        captured["kw"] = kw
        return len(entries), len(entries), []

    monkeypatch.setattr(app, "download_datasets", fake)
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "exp", "--force", "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert captured["n"] == 1
    assert captured["kw"] == {"force": True}


def test_check_out_without_json_writes_file(tmp_path: Path):
    app = make_app(
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(_head_ok))
    )
    out = tmp_path / "plan.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "check", "exp", "-o", str(tmp_path / "data"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["items"]
    assert '"items"' not in result.output


def test_check_out_write_failure_exits_1(tmp_path: Path):
    app = make_app(
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(_head_ok))
    )
    out = tmp_path / "isdir"
    out.mkdir()
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "check", "exp", "-o", str(tmp_path / "data"), "--out", str(out)],
    )
    assert result.exit_code == 1
    assert "Falha ao gravar o plano" in result.output
    assert "Traceback" not in result.output
    assert not list(tmp_path.glob(".*.tmp"))


def test_probe_sends_accept_encoding_identity(tmp_path: Path):
    seen: dict[str, str] = {}

    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(405)
        seen["ae"] = request.headers.get("Accept-Encoding", "")
        seen["range"] = request.headers.get("Range", "")
        return httpx2.Response(206, headers={"Content-Range": "bytes 0-0/10"})

    _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert seen == {"ae": "identity", "range": "bytes=0-0"}


def test_probe_200_with_content_encoding_returns_none(tmp_path: Path):
    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(405)
        return httpx2.Response(
            200, headers={"Content-Length": "50", "Content-Encoding": "gzip"}
        )

    app = _probe_app(handler)
    assert app._range_probe(ENTRY, tmp_path, ENTRY["url"], None) is None
    item = app.check_entry(ENTRY, tmp_path)
    assert item.reason.startswith("metadata-unavailable")


def test_head_403_probe_404_is_skip_unavailable(tmp_path: Path):
    def handler(request):
        return httpx2.Response(403 if request.method == "HEAD" else 404)

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.action == "skip-unavailable"
    assert item.reason == "remote-404"


def test_head_403_probe_404_with_fallback_mixed(tmp_path: Path):
    def handler(request):
        if request.url.path.endswith("alt.csv"):
            return httpx2.Response(404)  # HEAD 404 direto
        return httpx2.Response(403 if request.method == "HEAD" else 404)

    entry = {**ENTRY, "fallback_urls": ["https://example.test/alt.csv"]}
    item = _probe_app(handler).check_entry(entry, tmp_path)
    assert item.action == "skip-unavailable"
    assert item.reason == "remote-404"


def test_head_403_probe_500_stays_download(tmp_path: Path):
    def handler(request):
        return httpx2.Response(403 if request.method == "HEAD" else 500)

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason.startswith("metadata-unavailable")


@pytest.mark.parametrize(
    ("statuses", "action", "reason"),
    [
        # 503 transitório e depois 404 → removida de fato
        ([503, 404], "skip-unavailable", "remote-404"),
        # 429 e depois 206 → metadados pelo Range
        ([429, 206], "download", "range-probe"),
        # 503 persistente → desiste após as tentativas
        ([503, 503, 503], "download", "metadata-unavailable"),
        # 403 no GET não é transitório → sem nova tentativa
        ([403], "download", "metadata-unavailable"),
    ],
)
def test_probe_retries_transient_status(
    tmp_path: Path, monkeypatch, statuses, action, reason
):
    from quantilica.cli import sdk

    monkeypatch.setattr(sdk, "_PROBE_BACKOFF_S", 0.0)
    gets: list[int] = []

    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(403)
        status = statuses[min(len(gets), len(statuses) - 1)]
        gets.append(status)
        headers = {"Content-Range": "bytes 0-0/10"} if status == 206 else {}
        return httpx2.Response(status, headers=headers)

    item = _probe_app(handler).check_entry(ENTRY, tmp_path)
    assert item.action == action
    assert item.reason.startswith(reason)
    assert gets == statuses
