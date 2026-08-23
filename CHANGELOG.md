# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [0.3.2] - 2026-08-22

### Corrigido
- **Crítico:** wheels publicados desde a 0.3.0 vinham **sem os módulos do pacote** — o diretório `quantilica/cli/` não era incluído no build por configuração incorreta do hatchling (`sources` na seção global e `packages` apontando para o subpacote). Instalações via pip/uv reportavam sucesso, mas `import quantilica.cli` falhava em qualquer ambiente não-editable. Configuração realinhada ao padrão dos pacotes irmãos (`packages = ["src/quantilica"]` dentro de `[tool.hatch.build.targets.wheel]`).

## [0.3.1] - 2026-08-22

### Corrigido
- `DEFAULT_INDEX_URL` apontado para `https://index.quantilica.com/simple/` — a URL anterior (`quantilica.com/quantilica-index/`) parou de ser servida quando o domínio passou ao portal, quebrando o `quantilica install` para fetchers fora do PyPI legado (detalhes no ADR de distribuição de 2026-08-22).
- `install`/`uninstall` agora mesclam o registro remoto (`sources.json`) com o registro local, resolvendo também nomes canônicos do índice (ex.: `tesouro-direto`, além de `td`).

## [0.3.0] - 2026-08-10

### Adicionado
- Extensão da `FetcherApp` (`sdk.py`) com suporte opcional para `FtpClient`.
- Exposição do método `download_datasets` na `FetcherApp` para uso por comandos Typer customizados nos plugins.
- Aceitação e passagem do parâmetro `aliases_dict` para personalização total dos subcomandos por fetcher.

### Alterado
- Padrão de metadados do pacote portado integralmente para a PEP 639 (licença) e PEP 561 (tipagem estática).

## [0.2.2] - 2026-07-28
*(Release retroativo não documentado)*

## [0.2.0] - 2026-07-15
*(Release retroativo não documentado)*

## [0.1.0] - 2026-06-04

Primeira entrada em formato Keep a Changelog; documenta o estado do pacote nesta
versão.

### Adicionado

- CLI unificada `quantilica` que descobre e monta os fetchers instalados via
  entry points `quantilica.fetchers`, sem depender diretamente dos pacotes de
  fetcher.
- Comando `list-sources` e montagem automática dos sub-apps Typer de cada fetcher
  instalado (`quantilica <fonte> ...`).
