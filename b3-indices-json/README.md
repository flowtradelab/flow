# Índices da B3 em JSON

Base aberta e atualizável com os índices disponibilizados pela B3, seus ativos constituintes, pesos e a classificação setorial de cada emissor. O projeto gera um JSON por índice e um arquivo consolidado.

> Projeto independente, sem vínculo com a B3. Os dados são informativos e não constituem recomendação de investimento.

## Dados disponíveis

| Caminho | Conteúdo |
| --- | --- |
| `data/indices/IBOV.json` | Um índice, com metadados e constituintes |
| `data/consolidated.json` | Todos os índices em um único arquivo |
| `data/indexes.json` | Catálogo leve dos índices e arquivos |
| `data/companies.json` | Cadastro e classificação setorial dos emissores |
| `data/history/<ÍNDICE>/` | Fotografias por período de carteira e mudanças extraordinárias |

Os índices são descobertos automaticamente a partir da relação oficial “ações por índice”; portanto, a lista acompanha inclusões e retiradas feitas pela B3 sem depender de uma relação fixa mantida à mão.

### Estrutura de um constituinte

```json
{
  "ticker": "PETR4",
  "company": "PETROLEO BRASILEIRO S.A. PETROBRAS",
  "trading_name": "PETROBRAS",
  "asset_type": "PN N2",
  "theoretical_quantity": 4043349180,
  "weight_percent": 4.123,
  "sector": "Petróleo. Gás e Biocombustíveis",
  "subsector": "Petróleo. Gás e Biocombustíveis",
  "segment": "Exploração. Refino e Distribuição",
  "issuer_code": "PETR",
  "cvm_code": "9512",
  "cnpj": "33000167000101"
}
```

Quando a B3 não informa uma classificação para o emissor, `sector` recebe `"Não classificado pela B3"` e os níveis mais detalhados ficam como `null`; isso ocorre principalmente com determinados recibos, fundos ou emissores estrangeiros.

## Fontes e metodologia

O coletor usa páginas e endpoints JSON públicos que alimentam o portal oficial da B3:

- [Ações por índice](https://sistemaswebb3-listados.b3.com.br/indexPage/stocks): descobre os índices e a relação inicial entre códigos e carteiras.
- [Carteira do dia](https://sistemaswebb3-listados.b3.com.br/indexPage/day/IBOV?language=pt-br): fornece constituintes, quantidade teórica, participação e data de referência. O código do índice na URL é variável.
- [Empresas listadas](https://www.b3.com.br/pt_br/produtos-e-servicos/negociacao/renda-variavel/empresas-listadas.htm): fornece razão social, CNPJ, código CVM e classificação “setor / subsetor / segmento”.
- [Página institucional dos índices](https://www.b3.com.br/pt_br/market-data-e-indices/indices/): descreve as famílias, regras e metodologias de cada índice.

Fluxo de atualização:

1. Baixa a relação “ações por índice” e extrai os códigos únicos.
2. Consulta a carteira corrente de cada índice.
3. Relaciona o ticker ao emissor e busca sua classificação setorial oficial.
4. Normaliza números brasileiros (`1.234,56`) para números JSON.
5. Grava arquivos individuais, catálogo, cadastro de empresas e consolidado.
6. Preserva uma fotografia por quadrimestre; se a composição mudar dentro do período, também preserva a versão anterior datada.

Os endpoints usados são públicos e pertencem ao portal da B3, mas não constituem uma API pública formal com garantia de estabilidade. Mudanças no portal podem exigir ajustes no coletor. Consulte os termos de uso da B3 antes de redistribuir ou explorar os dados comercialmente.

## Atualização local

Requer Python 3.10 ou superior e não possui dependências externas.

```bash
python -m unittest discover -s tests -v
python scripts/update_data.py
```

Para atualizar somente um índice durante desenvolvimento:

```bash
python scripts/update_data.py --index IBOV
```

## Atualização automática

O workflow `.github/workflows/update-data.yml` roda em dias úteis às 18h30 no horário de Brasília e também pode ser iniciado manualmente na aba **Actions**. Quando há diferenças, ele cria um commit apenas com os arquivos de dados.

O workflow utiliza somente o `GITHUB_TOKEN` automático, com permissão `contents: write`; nenhum segredo adicional é necessário.

## Histórico

A B3 revisa grande parte das carteiras em ciclos quadrimestrais. O cabeçalho da fonte oficial informa o início e o fim da vigência, usados como identificador do snapshot (`AAAA-MM_MM`). Mudanças extraordinárias de constituintes dentro do mesmo período geram um arquivo adicional `before-AAAA-MM-DD`, preservando a composição anterior quando ela estava disponível no repositório.

O histórico começa na primeira execução deste projeto; ele não pretende reconstruir retroativamente todas as carteiras anteriores à criação do repositório.

## Licença

O código deste repositório é disponibilizado sob a licença MIT. Os dados de origem continuam sujeitos às regras e aos direitos da B3 e dos respectivos emissores.
