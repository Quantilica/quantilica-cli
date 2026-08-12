# quantilica-cli

CLI unificada para o ecossistema Quantilica de dados abertos brasileiros.
For full documentation, please visit [https://docs.quantilica.com](https://docs.quantilica.com).

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
