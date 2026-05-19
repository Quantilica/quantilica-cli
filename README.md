# quantilica-cli

CLI unificada para o ecossistema Quantilica de dados abertos brasileiros.

## Instalação

```bash
pip install quantilica-cli
```

Instale os fetchers desejados separadamente:

```bash
pip install comex-fetcher inmet-fetcher rtn-fetcher
```

## Uso

```bash
quantilica --help
quantilica list-sources
quantilica fetch comex sync 2024
quantilica fetch inmet sync 2020 2021
quantilica fetch td sync --dataset prices
```
