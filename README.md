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

### Resumo da coleta em JSON

`sync --summary-json ARQUIVO` grava, ao terminar, um JSON com os contadores que o
comando também imprime na linha `Concluído: X/Y arquivo(s) baixado(s)`, para quem
precisa deles sem ler texto (um agendador, um painel, um teste):

```bash
quantilica comex sync 2024 --summary-json resumo.json
```

```json
{
  "schema_version": 1,
  "fetcher": "comex-fetcher",
  "command": "sync",
  "status": "partial",
  "exit_code": 3,
  "total": 12, "succeeded": 11, "failed": 1,
  "downloaded": 4, "skipped": 7, "bytes_downloaded": 52428800,
  "errors": [{"id": "exp-2024", "message": "HTTP 503"}]
}
```

`succeeded` é o numerador de `Concluído` (inclui o que já estava em dia);
`downloaded`, `skipped` e `bytes_downloaded` separam o que foi transferido de fato,
e são `null` quando o fetcher baixa por código próprio. O arquivo é gravado de forma
atômica, também quando o comando falha (com o motivo em `error`), e a opção nunca muda
o código de saída. `pipeline --summary-json` repassa a opção ao passo de download.
O contrato completo está no docstring de `quantilica.cli.summary`.

## Lake

`quantilica lake` monta e audita o lake de Parquet a partir de um bruto local, sem
depender de banco de dados nem do ledger do acervo (ele é opcional). O fluxo é
bruto local → `select` → `build` → `audit`:

```bash
# 1. visão só com a versão vigente de cada arquivo (hardlink; cai em symlink e em cópia)
quantilica lake select --raw /dados/bruto --out /dados/vigente --exclude excluir.txt

# 2. conversão em Parquet, por fonte e dataset, em processos separados; retoma uma execução interrompida
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
