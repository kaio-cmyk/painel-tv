# Painel TV — Conta Junto

Painel operacional com as entregas (obrigações e tarefas) do mês por departamento, vindas do Acessórias.

- `index.html`: o painel. Lê o `dados.json` a cada minuto.
- `sincronizar.py`: busca as entregas na API do Acessórias e gera o `dados.json`.
- `.github/workflows/painel.yml`: roda o sincronizador a cada 10 min e publica no GitHub Pages.
- `.github/workflows/manter-ativo.yml`: commit vazio mensal para o GitHub não desligar a rotina.

O `dados.json` publicado só tem departamento, prazo e situação de cada entrega.

## Configuração

1. **Settings → Secrets and variables → Actions → New repository secret**: nome `ACESSORIAS_TOKEN`, valor = token da API do Acessórias.
2. **Settings → Pages → Build and deployment → Source**: `GitHub Actions`.
3. **Actions → Atualizar painel → Run workflow** (marque "Forçar busca completa" na primeira vez). Leva ~7 min.

Link do painel: `https://<usuario>.github.io/<repositorio>/`

## Rodar localmente

```
python sincronizar.py
python -m http.server 8770
```

Depois abra `http://localhost:8770`. Abrir o `index.html` com dois cliques não funciona: o navegador bloqueia a leitura do `dados.json`.
