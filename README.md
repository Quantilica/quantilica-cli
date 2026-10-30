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

## Lake

`quantilica lake` monta e audita o lake de Parquet a partir de um bruto local, sem
depender de banco de dados nem do ledger do acervo (ele é opcional). O fluxo é
bruto local → `select` → `build` → `audit`:

```bash
# 1. visão só com a versão vigente de cada arquivo (hardlink; cai em symlink e em cópia)
quantilica lake select --raw /dados/bruto --out /dados/vigente --exclude excluir.txt

# 2. conversão em Parquet, por fonte e dataset, em processos separados; retoma de onde parou
quantilica lake build --input /dados/vigente --lake /dados/lake --workers 4 --memory 12GB

# 3. auditoria: rodapés, proveniência (sha256), cobertura, estrutura e leitores
quantilica lake audit --lake /dados/lake --input /dados/vigente --json-file auditoria.json
```

- `select` só precisa do `quantilica-core`. `build` e `audit` usam o
  `quantilica-analytics`, que entra com `pip install "quantilica-cli[lake]"`; sem ele
  esses dois comandos avisam o que instalar e saem com código 1.
- `--exclude` e `--exceptions` aceitam um arquivo texto com um item por linha (linhas
  vazias e iniciadas por `#` são ignoradas): `sha256` ou caminho relativo no `select`,
  caminho relativo ou padrão `fnmatch` no `audit`.
- `build` sai com código 1 se alguma tarefa falhar, e com 130 se for interrompido
  (Ctrl+C); rodar de novo retoma e `--retry-errors` refaz só o que falhou. O andamento
  sai em stderr.
- `audit` sai com 0 se o lake for aprovado e com 1 se houver achados. `--json` imprime
  o relatório em JSON no stdout (o andamento fica em stderr); `--json-file ARQ` o grava.
- `--ledger ARQ` (opcional, só em `audit`) confere a proveniência também contra uma
  exportação do ledger; sem ele, a conferência usa o `sha256` dos manifestos ou do
  próprio arquivo.
