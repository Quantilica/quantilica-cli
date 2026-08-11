# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

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
