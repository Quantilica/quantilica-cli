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
quantilica comex sync 2024
quantilica inmet sync 2020 2021
quantilica td sync --dataset prices
```
