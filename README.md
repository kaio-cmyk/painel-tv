# Painel TV — Conta Junto

Painel para a TV com duas telas que se alternam a cada 2 minutos:

1. **Painel Operacional**: entregas (obrigações e tarefas) do mês por departamento, vindas do Acessórias.
2. **Satisfação do Cliente**: avaliações dos clientes (notas de 1 a 5) somadas de dois canais: a pesquisa
   do WhatsApp (Bradial) e a nota dada ao finalizar uma solicitação no Acessórias. Mês atual comparado com o
   anterior. Se um canal falhar, a tela segue com o outro; sem nenhum, a TV fica só na primeira tela.

- `index.html`: o painel. Lê o `dados.json` e o `csat.json` a cada minuto.
- `sincronizar.py`: busca as entregas na API do Acessórias e gera o `dados.json`.
- `csat.py`: busca as avaliações no Bradial e no Acessórias e gera o `csat.json`.
- `.github/workflows/painel.yml`: roda os dois a cada 10 min e publica no GitHub Pages.
- `.github/workflows/manter-ativo.yml`: commit vazio mensal para o GitHub não desligar a rotina.

O `dados.json` publicado só tem departamento, prazo e situação de cada entrega. O `csat.json` só tem
números, notas, comentários (como o cliente escreveu, inclusive o nome do colaborador que ele citou),
data e canal: sem ranking por colaborador e nunca o nome, o telefone ou a empresa do cliente.
Taxa de resposta = avaliações ÷ (pesquisas enviadas no WhatsApp + solicitações externas finalizadas).

## Configuração

1. **Settings → Secrets and variables → Actions → New repository secret**:
   - `ACESSORIAS_TOKEN` = token da API do Acessórias;
   - `BRADIAL_API_KEY` = chave da API do Bradial.
2. **Settings → Pages → Build and deployment → Source**: `GitHub Actions`.
3. **Actions → Atualizar painel → Run workflow** (marque "Forçar busca completa" na primeira vez). Leva ~7 min.

Link do painel: `https://<usuario>.github.io/<repositorio>/`

Para testar as telas: `?tela=csat` fixa a tela de CSAT; `?rotacao=10` troca a cada 10 segundos.

## Rodar localmente

```
python sincronizar.py
python csat.py
python -m http.server 8770
```

Depois abra `http://localhost:8770`. Abrir o `index.html` com dois cliques não funciona: o navegador bloqueia a leitura do `dados.json`.
