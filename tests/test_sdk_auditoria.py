"""Regras do host contra falhas silenciosas dos fetchers (auditoria de 11/2026).

1. catálogo vazio não é sucesso (``sync``/``check``) e ``expected_empty_groups``;
2. 404 de entrada marcada com ``pending_publication``;
3. ``pipeline`` chama o ``sync`` efetivo, só com os argumentos que ele aceita;
4. ``convert``/``pipeline`` com diretório de entrada ausente;
5. checkpoint do ``backfill`` descarta falhas de unidades que não existem mais.

Sem rede: plugins de teste mínimos e ``httpx2.MockTransport``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import httpx2
import typer
from quantilica.core.http import HttpClient, HttpStatusError
from typer.testing import CliRunner

from quantilica.cli.sdk import (
    EXIT_SYNC_PARTIAL,
    FetcherApp,
)
from tests.test_sdk import GROUPS, _grouped, make_app, sample_list_datasets

runner = CliRunner()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _counting(app: FetcherApp, calls: list[int]) -> None:
    def download(entries, output_dir, workers=4):
        calls.append(len(entries))
        return len(entries), len(entries), []

    app.download_datasets = download  # type: ignore[method-assign]


# ---------------------------------------------------------------
# 1. catálogo vazio
# ---------------------------------------------------------------


def _only(*full: str):
    """``list_datasets`` que só devolve entradas para os grupos dados."""

    def listing(group: str) -> list[dict]:
        return sample_list_datasets(group) if group in full else []

    return listing


def test_sync_catalogo_vazio_sai_com_um_e_diz_os_grupos(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only())
    calls: list[int] = []
    _counting(app, calls)
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 1, result.output
    assert "catálogo vazio para exp, imp" in " ".join(result.output.split())
    assert calls == [0]  # o download do plugin roda com a lista vazia
    data = _load(out)
    assert data["status"] == "failed" and data["exit_code"] == 1
    assert data["error"] == "catálogo vazio para exp, imp"
    assert data["empty_groups"] == ["exp", "imp"]


def test_catalogo_vazio_nao_apaga_os_erros_do_download_do_plugin(
    tmp_path: Path,
) -> None:
    """Plugin que soma causas próprias (ex.: falha de descoberta) aos erros."""
    app = make_app(list_datasets=_only())

    def download(entries, output_dir, workers=4):
        return 0, 1, [("descoberta:exp", "listagem fora do ar")]

    app.download_datasets = download  # type: ignore[method-assign]
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 1, result.output
    saida = " ".join(result.output.split())
    assert "descoberta:exp: listagem fora do ar" in saida
    assert "catálogo vazio para exp, imp" in saida
    data = _load(out)
    assert data["errors"] == [
        {"id": "descoberta:exp", "message": "listagem fora do ar"}
    ]
    assert data["failed"] == 1 and data["total"] == 1
    assert data["error"] == "catálogo vazio para exp, imp"
    assert data["exit_code"] == 1


def test_catalogo_vazio_no_dry_run_nao_chama_o_download(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only())
    calls: list[int] = []
    _counting(app, calls)
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--dry-run", "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert calls == []


def test_check_catalogo_vazio_deixa_o_plugin_abortar_com_a_causa_dele(
    tmp_path: Path,
) -> None:
    app = make_app(list_datasets=_only())
    visto: list[int] = []

    def check_datasets(entries, output_dir, workers=4):
        visto.append(len(entries))
        raise typer.Exit(1)

    app.check_datasets = check_datasets  # type: ignore[method-assign]
    result = runner.invoke(app.app, ["check", "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert visto == [0]


def test_sync_catalogo_vazio_so_para_o_grupo_pedido(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only("exp"))
    calls: list[int] = []
    _counting(app, calls)
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "imp", "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "catálogo vazio para imp" in result.output
    ok = runner.invoke(_grouped(app.app), ["fd", "sync", "exp", "-o", str(tmp_path)])
    assert ok.exit_code == 0, ok.output


def test_sync_catalogo_vazio_vale_tambem_no_dry_run(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only())
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--dry-run", "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "catálogo vazio" in result.output


def test_sync_grupo_vazio_com_outros_cheios_avisa_e_nao_falha(
    tmp_path: Path,
) -> None:
    app = make_app(list_datasets=_only("exp"))
    calls: list[int] = []
    _counting(app, calls)
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert calls == [1]
    saida = " ".join(result.output.split())
    assert "WARNING" in saida and "grupo(s) sem entradas: imp" in saida
    data = _load(out)
    assert data["status"] == "success" and data["error"] is None
    assert data["empty_groups"] == ["imp"]


def test_sync_sem_grupo_vazio_tem_empty_groups_vazio(tmp_path: Path) -> None:
    app = make_app()
    _counting(app, [])
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 0, result.output
    data = _load(out)
    assert data["empty_groups"] == [] and data["skipped_items"] == []


def test_grupo_declarado_vazio_por_desenho_nao_avisa(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only("exp"), expected_empty_groups=["imp"])
    _counting(app, [])
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
    assert _load(out)["empty_groups"] == []


def test_so_grupos_declarados_vazios_pedidos_sai_com_zero(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only(), expected_empty_groups=["imp"])
    _counting(app, [])
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "imp", "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_declarado_mas_catalogo_todo_vazio_falha_pelo_nao_declarado(
    tmp_path: Path,
) -> None:
    app = make_app(list_datasets=_only(), expected_empty_groups=["imp"])
    _counting(app, [])
    result = runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "catálogo vazio para exp" in result.output


def test_declarar_grupo_nao_esconde_grupo_que_esvaziou(tmp_path: Path) -> None:
    """A declaração vale por grupo: o 'exp', não declarado, continua avisando."""
    app = make_app(list_datasets=_only("imp"), expected_empty_groups=["imp"])
    _counting(app, [])
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "r"), "--summary-json", str(out)],
    )
    assert result.exit_code == 0
    assert _load(out)["empty_groups"] == ["exp"]


def test_check_catalogo_vazio_sai_com_um(tmp_path: Path) -> None:
    app = make_app(list_datasets=_only())
    result = runner.invoke(app.app, ["check", "-o", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert "catálogo vazio para exp, imp" in result.output


def test_from_plan_sem_nenhuma_entrada_verificada_falha(tmp_path: Path) -> None:
    app = make_app()
    _counting(app, [])
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"fetcher": "x", "output_dir": "", "generated_at": "", "items": []}),
        encoding="utf-8",
    )
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--from-plan", str(plan), "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "plano sem nenhuma entrada" in " ".join(result.output.split())


def test_check_com_grupos_declarados_vazios_grava_no_plano_e_o_sync_aceita(
    tmp_path: Path,
) -> None:
    app = make_app(list_datasets=_only(), expected_empty_groups=["exp", "imp"])
    _counting(app, [])
    plan = tmp_path / "plan.json"
    checked = runner.invoke(app.app, ["check", "-o", str(tmp_path), "--out", str(plan)])
    assert checked.exit_code == 0, checked.output
    data = _load(plan)
    assert data["items"] == [] and data["expected_empty"] is True
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--from-plan", str(plan), "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_plano_vazio_sem_a_marca_continua_falhando_no_sync(tmp_path: Path) -> None:
    """Plano que sobrevive a outro processo (chave preservada) ou não: sem marca, erro."""
    app = make_app(list_datasets=_only(), expected_empty_groups=["exp", "imp"])
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"fetcher": "x", "output_dir": "", "generated_at": "", "items": []}),
        encoding="utf-8",
    )
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--from-plan", str(plan), "-o", str(tmp_path)]
    )
    assert result.exit_code == 1


def test_plano_com_entradas_nao_grava_expected_empty(tmp_path: Path) -> None:
    app = make_app(client=_not_found_client())
    plan = tmp_path / "plan.json"
    runner.invoke(app.app, ["check", "-o", str(tmp_path), "--out", str(plan)])
    assert "expected_empty" not in _load(plan)


def test_from_plan_com_tudo_em_dia_continua_saindo_com_zero(tmp_path: Path) -> None:
    app = make_app()
    _counting(app, [])
    entry = sample_list_datasets("exp")[0]
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "fetcher": "x",
                "output_dir": "",
                "generated_at": "",
                "items": [
                    {
                        "dataset": "exp",
                        "id": "exp-monthly",
                        "entry": entry,
                        "action": "skip-up-to-date",
                        "reason": "local-fresh",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "--from-plan", str(plan), "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------
# 2. 404 de entrada ainda não publicada
# ---------------------------------------------------------------


def _not_found_client(*, missing: str = "") -> HttpClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if missing and missing in str(request.url):
            return httpx2.Response(404)
        if request.method == "HEAD":
            return httpx2.Response(
                200,
                headers={
                    "Content-Length": "4",
                    "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                },
            )
        return httpx2.Response(200, content=b"data")

    return HttpClient(attempts=1, transport=httpx2.MockTransport(handler))


def _catalog(*, pending: bool) -> Any:
    def listing(group: str) -> list[dict]:
        entries = sample_list_datasets(group)
        if group == "imp":
            entries[0]["id"] = "imp-novo"
            entries[0]["url"] = "https://example.test/imp-novo.csv"
            if pending:
                entries[0]["pending_publication"] = True
        return entries

    return listing


def test_404_de_entrada_marcada_nao_e_erro_e_conta_como_pulada(
    tmp_path: Path,
) -> None:
    app = make_app(
        list_datasets=_catalog(pending=True),
        client=_not_found_client(missing="imp-novo"),
    )
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "sync",
            "-o",
            str(tmp_path / "raw"),
            "--summary-json",
            str(out),
            "--verbose",  # o INFO só aparece com --verbose
        ],
    )
    assert result.exit_code == 0, result.output
    data = _load(out)
    assert data["status"] == "success"
    assert data["total"] == 2 and data["failed"] == 0 and data["errors"] == []
    assert data["downloaded"] == 1 and data["skipped"] == 1
    assert data["skipped_items"] == [
        {"id": "imp-novo", "reason": "pending-publication"}
    ]
    assert "ainda não publicada" in " ".join(result.output.split())
    assert "INFO" in result.output and "ainda não publicado pela fonte" in " ".join(
        result.output.split()
    )


def test_404_sem_a_marca_continua_sendo_erro(tmp_path: Path) -> None:
    app = make_app(
        list_datasets=_catalog(pending=False),
        client=_not_found_client(missing="imp-novo"),
    )
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    data = _load(out)
    assert data["failed"] == 1 and data["skipped_items"] == []


def test_so_entradas_pendentes_404_sai_com_zero(tmp_path: Path) -> None:
    def listing(group: str) -> list[dict]:
        return [{**e, "pending_publication": True} for e in sample_list_datasets(group)]

    app = make_app(list_datasets=listing, client=_not_found_client(missing="example"))
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "-o", str(tmp_path / "raw")]
    )
    assert result.exit_code == 0, result.output


def test_marca_nao_perdoa_erro_diferente_de_404(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403)

    app = make_app(
        list_datasets=_catalog(pending=True),
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(handler)),
    )
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "-o", str(tmp_path / "raw")]
    )
    assert result.exit_code == 1, result.output  # nada baixou, todos erros


def test_marca_perdoa_404_vindo_de_download_entry_proprio(tmp_path: Path) -> None:
    app = make_app(list_datasets=_catalog(pending=True))

    def download_entry(entry, output_dir, **kwargs):
        if entry["id"] == "imp-novo":
            raise HttpStatusError(entry["url"], 404)
        return output_dir / entry["id"]

    app.download_entry = download_entry  # type: ignore[method-assign]
    result = runner.invoke(
        _grouped(app.app), ["fd", "sync", "-o", str(tmp_path / "raw")]
    )
    assert result.exit_code == 0, result.output


def test_404_na_url_principal_e_5xx_na_alternativa_nao_e_perdoado(
    tmp_path: Path,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if "imp-novo" in url and "alternativa" not in url:
            return httpx2.Response(404)
        if "alternativa" in url:
            return httpx2.Response(503)
        if request.method == "HEAD":
            return httpx2.Response(200, headers={"Content-Length": "4"})
        return httpx2.Response(200, content=b"data")

    def listing(group: str) -> list[dict]:
        entries = sample_list_datasets(group)
        if group == "imp":
            entries[0].update(
                id="imp-novo",
                url="https://example.test/imp-novo.csv",
                fallback_urls=["https://example.test/alternativa/imp-novo.csv"],
                pending_publication=True,
            )
        return entries

    app = make_app(
        list_datasets=listing,
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(handler)),
    )
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    data = _load(out)
    assert [e["id"] for e in data["errors"]] == ["imp-novo"]
    assert data["skipped_items"] == []


def test_erro_levantado_durante_o_tratamento_de_um_404_nao_e_um_404() -> None:
    from quantilica.cli.sdk import _is_not_found

    try:
        try:
            raise HttpStatusError("https://example.test/x", 404)
        except HttpStatusError:
            raise TimeoutError("tempo esgotado") from None  # sem __cause__
    except TimeoutError as exc:
        assert exc.__context__ is not None
        assert _is_not_found(exc) is False
    try:
        try:
            raise HttpStatusError("https://example.test/x", 404)
        except HttpStatusError as inner:
            raise RuntimeError("embrulho") from inner
    except RuntimeError as exc:
        assert _is_not_found(exc) is True


def test_check_devolve_pending_publication_para_entrada_marcada(
    tmp_path: Path,
) -> None:
    app = make_app(
        list_datasets=_catalog(pending=True),
        client=_not_found_client(missing="imp-novo"),
    )
    plan = app.check_datasets(
        [e for g in GROUPS for e in app.list_datasets(g)], tmp_path, workers=1
    )
    by_id = {i.id: i for i in plan.items}
    assert by_id["imp-novo"].action == "skip-unavailable"
    assert by_id["imp-novo"].reason == "pending-publication"
    assert by_id["exp-monthly"].action == "download"


def test_check_sem_a_marca_segue_remote_404(tmp_path: Path) -> None:
    app = make_app(
        list_datasets=_catalog(pending=False),
        client=_not_found_client(missing="imp-novo"),
    )
    item = app.check_entry(app.list_datasets("imp")[0], tmp_path)
    assert (item.action, item.reason) == ("skip-unavailable", "remote-404")


# ---------------------------------------------------------------
# 3. pipeline chama o sync efetivo
# ---------------------------------------------------------------


def _conv(calls: list[tuple[Path, Path]] | None = None):
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        if calls is not None:
            calls.append((data_dir, parquet_dir))

    return conv


def test_pipeline_usa_o_sync_do_plugin_registrado_depois_do_padrao(
    tmp_path: Path,
) -> None:
    """O padrão do FetcherApp é registrado primeiro; o do plugin vence no typer."""
    app = make_app()
    seen: list[dict[str, Any]] = []

    @app.app.command("sync")
    def cmd_sync(
        datasets: Annotated[list[str] | None, typer.Argument()] = None,
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
        threads: Annotated[int, typer.Option("--threads")] = 2,
        verbose: Annotated[bool, typer.Option("--verbose")] = False,
    ) -> None:
        seen.append(
            {"datasets": datasets, "output": output, "threads": threads, "v": verbose}
        )

    raw = tmp_path / "raw"
    raw.mkdir()
    calls: list[tuple[Path, Path]] = []
    app.command_pipeline(_conv(calls))

    # a CLI resolve "sync" para o do plugin, e o pipeline também
    root = _grouped(app.app)
    result = runner.invoke(
        root,
        ["fd", "pipeline", "exp", "-o", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code == 0, result.output
    assert seen == [{"datasets": ["exp"], "output": raw, "threads": 2, "v": False}]
    assert calls == [(raw, tmp_path / "lake")]


def test_pipeline_com_sync_proprio_de_assinatura_diferente_nao_da_typeerror(
    tmp_path: Path,
) -> None:
    """O caso do ``tse``: sync próprio sem ``groups``/``workers``."""
    app = make_app(build_default_commands=False)
    seen: list[dict[str, Any]] = []

    @app.app.command("sync")
    def cmd_sync(
        datasets: Annotated[list[str] | None, typer.Argument()] = None,
        years: Annotated[list[str] | None, typer.Option("-y", "--years")] = None,
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
        verbose: Annotated[bool, typer.Option("--verbose")] = False,
    ) -> None:
        seen.append({"datasets": datasets, "years": years, "dry_run": dry_run})

    raw = tmp_path / "raw"
    raw.mkdir()
    app.command_pipeline(_conv())
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "pipeline",
            "-o",
            str(raw),
            "--lake",
            str(tmp_path / "l"),
            "--workers",
            "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [{"datasets": None, "years": None, "dry_run": False}]
    assert "não aceita --workers" in result.output  # aviso em stderr (runner mistura)


def test_pipeline_dry_run_vai_ao_sync_proprio_e_nao_converte(tmp_path: Path) -> None:
    app = make_app(build_default_commands=False)
    seen: list[bool] = []

    @app.app.command("sync")
    def cmd_sync(
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    ) -> None:
        seen.append(dry_run)

    calls: list[tuple[Path, Path]] = []
    app.command_pipeline(_conv(calls))
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert seen == [True] and calls == []


def test_pipeline_com_sync_sem_dry_run_recusa_em_vez_de_baixar_de_verdade(
    tmp_path: Path,
) -> None:
    app = make_app(build_default_commands=False)
    ran: list[int] = []

    @app.app.command("sync")
    def cmd_sync(
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
    ) -> None:
        ran.append(1)

    app.command_pipeline(_conv())
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "--dry-run"])
    assert result.exit_code == 2
    assert "não aceita --dry-run" in " ".join(result.output.split())
    assert ran == []


def test_pipeline_com_sync_sem_output_recusa_com_mensagem_clara(tmp_path: Path) -> None:
    app = make_app(build_default_commands=False)

    @app.app.command("sync")
    def cmd_sync(verbose: bool = False) -> None:
        raise AssertionError("não devia rodar")

    app.command_pipeline(_conv())
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline"])
    assert result.exit_code == 2
    assert "não aceita -o/--output" in " ".join(result.output.split())
    assert not isinstance(result.exception, TypeError)


def test_pipeline_com_grupos_e_sync_sem_argumento_variadico_recusa(
    tmp_path: Path,
) -> None:
    app = make_app(build_default_commands=False)

    @app.app.command("sync")
    def cmd_sync(
        agregado_id: Annotated[int | None, typer.Argument()] = None,
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
    ) -> None:
        raise AssertionError("não devia rodar")

    app.command_pipeline(_conv())
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "exp"])
    assert result.exit_code == 2
    assert "não aceita grupos" in " ".join(result.output.split())


def test_pipeline_com_sync_de_opcao_obrigatoria_recusa(tmp_path: Path) -> None:
    app = make_app(build_default_commands=False)

    @app.app.command("sync")
    def cmd_sync(
        token: Annotated[str, typer.Option("--token")],
        output: Annotated[Path, typer.Option("-o", "--output")] = tmp_path,
    ) -> None:
        raise AssertionError("não devia rodar")

    app.command_pipeline(_conv())
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline"])
    assert result.exit_code == 2
    assert "--token" in result.output
    assert not isinstance(result.exception, TypeError)


def test_pipeline_com_sync_padrao_continua_passando_tudo(tmp_path: Path) -> None:
    app = make_app()
    seen: list[tuple[int, int]] = []

    def download(entries, output_dir, workers=4):
        seen.append((len(entries), workers))
        return len(entries), len(entries), []

    app.download_datasets = download  # type: ignore[method-assign]
    raw = tmp_path / "raw"
    raw.mkdir()
    app.command_pipeline(_conv())
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "pipeline",
            "exp",
            "--workers",
            "3",
            "-o",
            str(raw),
            "--lake",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [(1, 3)]


# ---------------------------------------------------------------
# 4. convert com entrada ausente
# ---------------------------------------------------------------


def test_convert_com_diretorio_de_entrada_ausente_sai_com_um(tmp_path: Path) -> None:
    app = make_app()
    called: list[int] = []

    @app.command_convert
    def conv(input: Path, lake: Path) -> Any:
        called.append(1)
        return {"converted": 0, "skipped": 0, "errors": 0}  # "sucesso" vazio

    missing = tmp_path / "nao-existe"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(missing), "--lake", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "diretório de entrada do conversor não existe" in " ".join(
        result.output.split()
    )
    assert called == []


def test_convert_com_diretorio_existente_e_vazio_segue_como_antes(
    tmp_path: Path,
) -> None:
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path) -> Any:
        return {"converted": 0, "skipped": 0, "errors": 0}

    (tmp_path / "raw").mkdir()
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(tmp_path / "raw"), "--lake", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output


def test_pipeline_sem_diretorio_de_saida_depois_do_sync_sai_com_um(
    tmp_path: Path,
) -> None:
    app = make_app()
    _counting(app, [])  # o "download" não cria a pasta
    called: list[int] = []

    def conv(data_dir: Path, parquet_dir: Path) -> None:
        called.append(1)

    app.command_pipeline(conv)
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "pipeline", "-o", str(tmp_path / "raw"), "--lake", str(tmp_path / "l")],
    )
    assert result.exit_code == 1
    assert "diretório de entrada do conversor não existe" in " ".join(
        result.output.split()
    )
    assert called == []


# ---------------------------------------------------------------
# 5. checkpoint do backfill
# ---------------------------------------------------------------


def test_backfill_descarta_falha_de_unidade_que_nao_existe_mais(
    tmp_path: Path,
) -> None:
    app = make_app()
    state = {"fase": 1}
    ran: list[str] = []

    def units() -> list[str]:
        # 1ª execução: a listagem falha e o plugin gera uma unidade de erro
        return ["mega:ultimo"] if state["fase"] == 1 else ["mega:1", "mega:2"]

    def run_unit(unit: str) -> None:
        if unit == "mega:ultimo":
            raise RuntimeError("listagem falhou")
        ran.append(unit)

    app.command_backfill(units, run_unit, name="backfill")
    first = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert first.exit_code == 1
    cp = tmp_path / ".backfill" / "backfill.json"
    assert "mega:ultimo" in json.loads(cp.read_text(encoding="utf-8"))["failed"]

    state["fase"] = 2
    second = runner.invoke(  # --resume é o padrão
        app.app, ["backfill", "-o", str(tmp_path), "--verbose"]
    )
    assert second.exit_code == 0, second.output
    assert ran == ["mega:1", "mega:2"]
    assert json.loads(cp.read_text(encoding="utf-8"))["failed"] == {}
    assert "descartada(s): mega:ultimo" in " ".join(second.output.split())

    third = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert third.exit_code == 0


def test_backfill_mantem_falha_de_unidade_que_ainda_esta_na_lista(
    tmp_path: Path,
) -> None:
    app = make_app()

    def run_unit(unit: str) -> None:
        if unit == "bad":
            raise RuntimeError("boom")

    app.command_backfill(lambda: ["good", "bad"], run_unit, name="backfill")
    assert runner.invoke(app.app, ["backfill", "-o", str(tmp_path)]).exit_code == 1
    again = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert again.exit_code == 1  # ainda está na lista e continua falhando
    cp = tmp_path / ".backfill" / "backfill.json"
    assert "bad" in json.loads(cp.read_text(encoding="utf-8"))["failed"]
