# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [0.1.0] - 2026-06-04

Primeira entrada em formato Keep a Changelog; documenta o estado do pacote nesta
versão.

### Adicionado

- CLI unificada `quantilica` que descobre e monta os fetchers instalados via
  entry points `quantilica.fetchers`, sem depender diretamente dos pacotes de
  fetcher.
- Comando `list-sources` e montagem automática dos sub-apps Typer de cada fetcher
  instalado (`quantilica <fonte> ...`).
