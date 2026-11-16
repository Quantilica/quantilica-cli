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

### Lake numa pasta sincronizada

Quando o lake mora numa pasta sincronizada por um cliente de nuvem, uma máquina
constrói e audita ali, e outra lê a mesma pasta sem nunca escrever nela. O cliente
envia os arquivos em ordem qualquer, então quem lê precisa de um critério próprio de
"publicado":

- `build` grava `<lake>/<fonte>/_em-atualizacao.json` antes de escrever a fonte, e
  `<lake>/<fonte>/_build.json` (o manifesto: `build_id` e o tamanho de todos os
  Parquets da fonte) ao fim de um build bem-sucedido. Um build que falha na fonte não
  grava o manifesto e mantém o marcador.
- Na máquina que constrói, `audit --limpar-marcador` remove o marcador das fontes cuja
  auditoria passa.
- Na máquina que lê, `audit --somente-leitura --estado ESTADO.json` não escreve nada
  no lake e confere o manifesto (todo arquivo listado existe com o mesmo tamanho, e
  não há Parquet fora dele). Por fonte, o `ESTADO.json` (fora do lake) recebe o
  `build_id` auditado e o resultado: `publicado`, `em_atualizacao` (há marcador; não
  reprova), `incompleto` (faltou arquivo, tamanho diferente, Parquet extra ou sem
  `_build.json`) ou `falhou` (outro achado).
- `catalog sync` e `docs schema` aceitam `--estado ESTADO.json` (ou a variável
  `QUANTILICA_LAKE_ESTADO`): só leem as fontes `publicado` cujo `build_id` é o do
  `_build.json` atual **e** cujo disco bate com esse manifesto na hora da leitura (cada
  Parquet listado existe com o mesmo tamanho e não há Parquet fora da lista; o cliente
  de sincronização pode ter entregue arquivos de um build novo depois da auditoria).
  As demais são puladas com aviso. Quem lê deve sempre usar o estado (de preferência
  pela variável de ambiente): sem ele só o marcador protege a leitura, e um lake sem
  `_build.json` segue como antes.
- `--estado` do `audit` só vale com `--somente-leitura` (exit 2 sem ele). O manifesto
  só vale para o que o `build` escreveu: Parquets gravados por outros caminhos (os
  `convert` dos fetchers) não o têm, e a auditoria `--somente-leitura` chama essa fonte
  de `incompleto`. O manifesto compara nomes e tamanhos, não o conteúdo: um arquivo
  reescrito com o mesmo tamanho passa despercebido.
