# .github/workflows/update_options.yml
name: Atualizar Opções B3

on:
  schedule:
    - cron: '0 10 * * 1-5'    # 7h00 BRT (UTC-3 = UTC+0 10h)
    - cron: '45 11 * * 1-5'   # 8h45 BRT
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Instalar dependências
        run: |
          pip install playwright requests pdfplumber
          playwright install chromium --with-deps

      - name: Baixar e processar opções B3
        id: update
        run: python scripts/update_options.py

      - name: Commit e push
        if: steps.update.outputs.updated == 'true' || steps.update.outputs.updated == 'false'
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add grid-options/ logs/
          # Só faz commit se houver mudanças staged
          git diff --staged --quiet || git commit -m "options: ${{ steps.update.outputs.data_date }} | $(date -u +'%H:%Mz')"
          git push || true

      - name: Data antiga — aguardando retry
        if: steps.update.outputs.updated == 'stale'
        run: |
          echo "::warning::Arquivo com data antiga. Próxima tentativa às 8h45."
          # Mesmo assim commita o log de execução
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add logs/ || true
          git diff --staged --quiet || git commit -m "log: data antiga ${{ steps.update.outputs.data_date }}"
          git push || true
          exit 0
