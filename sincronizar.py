"""Gera o dados.json do Painel TV a partir das entregas do Acessórias.

Roda no GitHub Actions a cada 10 min (.github/workflows/painel.yml) e também localmente:
  python sincronizar.py [--anterior dados.json] [--saida dados.json] [--completo]

Entregas = obrigações e tarefas com prazo no mês atual. O arquivo publicado só leva
departamento, datas (prazo técnico, atraso, entrega) e situação: nada de cliente, CNPJ ou responsável.

  - completo: empresa por empresa (a API só lista o escritório inteiro para alterações de
    ontem/hoje). Roda sem arquivo anterior, a cada 3 h e na virada do mês. Leva ~8 min.
  - incremental: só as entregas alteradas desde a última rodada. Leva segundos.

Token: variável ACESSORIAS_TOKEN; localmente, cai para o .mcp.json da raiz do projeto.
Datas no horário de Brasília (no Actions, TZ=America/Sao_Paulo).
"""
import argparse
import calendar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.acessorias.com"
INTERVALO_COMPLETO = timedelta(hours=3)
# Sobe quando muda o formato das entregas: força a busca completa para refazer todas.
VERSAO = 2
# No máximo 1 chamada por segundo (~60/min): folga no limite de 100/min, que o MCP e o
# P.I.V.O. também usam. Cada chamada leva ~3 s, então 4 em paralelo para chegar nesse ritmo.
PAUSA_REQ = 1.0
PARALELO = 4

_ritmo_lock = threading.Lock()
_proxima_chamada = 0.0


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def token():
    if os.environ.get("ACESSORIAS_TOKEN"):
        return os.environ["ACESSORIAS_TOKEN"].strip()
    mcp = Path(__file__).resolve().parent.parent / ".mcp.json"
    if mcp.exists():
        cfg = json.loads(mcp.read_text(encoding="utf-8"))
        return cfg["mcpServers"]["acessorias"]["headers"]["Authorization"].split()[-1]
    sys.exit("Defina a variável ACESSORIAS_TOKEN.")


TOKEN = token()


def aguardar_vez():
    global _proxima_chamada
    with _ritmo_lock:
        agora = time.monotonic()
        espera = max(0.0, _proxima_chamada - agora)
        _proxima_chamada = max(agora, _proxima_chamada) + PAUSA_REQ
    time.sleep(espera)


def chamar(caminho, **query):
    url = f"{BASE}/{caminho}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    for _ in range(4):
        aguardar_vez()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                texto = r.read().decode("utf-8")
            try:
                return json.loads(texto)
            except json.JSONDecodeError:
                return None  # empresa sem entregas volta corpo vazio
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log("Limite de requisições atingido; aguardando 60 s.")
                time.sleep(60)
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10)
    raise RuntimeError(f"Acessórias não respondeu: {caminho}")


def limites_do_mes(hoje):
    ultimo = calendar.monthrange(hoje.year, hoje.month)[1]
    return hoje.replace(day=1), hoje.replace(day=ultimo)


def entregas_da_resposta(res):
    """A API devolve um objeto (uma empresa) ou uma lista de empresas, cada uma com 'Entregas'."""
    if isinstance(res, dict):
        res = [res]
    if not isinstance(res, list):
        return []
    return [(emp, e) for emp in res if isinstance(emp, dict) for e in (emp.get("Entregas") or [])]


def converter(emp, e):
    cfg = e.get("Config") or {}
    entregue = (e.get("EntDtEntrega") or "0000-00-00") != "0000-00-00"
    if entregue:
        sit = "C"
    elif (e.get("Status") or "").lower().startswith("dispens"):
        sit = "D"
    else:
        sit = "A"
    chave = cfg.get("EntID") or f'{emp.get("ID")}|{e.get("Nome")}|{e.get("EntCompetencia")}|{e.get("EntDtPrazo")}'
    # p = prazo técnico (meta interna); a = data em que vira "Atrasada!"; e = dia da entrega
    item = {"d": cfg.get("DptoNome") or "Sem departamento", "p": e.get("EntDtPrazo"),
            "a": e.get("EntDtAtraso") or e.get("EntDtPrazo"), "s": sit}
    if entregue:
        item["e"] = e["EntDtEntrega"]
    return str(chave), item


def paginar_entregas(caminho, **query):
    """50 entregas por página; segue até vir página vazia ou incompleta."""
    pagina, itens = 1, []
    while pagina <= 200:
        lote = entregas_da_resposta(chamar(caminho, **query, Pagina=pagina))
        itens.extend(lote)
        if len(lote) < 50:
            break
        pagina += 1
    return itens


def listar_empresas():
    empresas, pagina = [], 1
    while True:
        res = chamar("companies/ListAll", Pagina=pagina)
        if not isinstance(res, list) or not res:
            return empresas
        empresas.extend(res)
        pagina += 1


def sync_completo(ini, fim):
    empresas = [c for c in listar_empresas() if re.sub(r"\D", "", c.get("Identificador") or "")]
    log(f"Sincronização completa de {ini:%m/%Y}: {len(empresas)} empresas.")

    def da_empresa(emp):
        doc = re.sub(r"\D", "", emp["Identificador"])
        return paginar_entregas(f"deliveries/{doc}", DtInitial=ini.isoformat(),
                                DtFinal=fim.isoformat(), config="")

    entregas = {}
    with ThreadPoolExecutor(PARALELO) as pool:
        for i, lote in enumerate(pool.map(da_empresa, empresas), 1):
            for emp, e in lote:
                chave, item = converter(emp, e)
                entregas[chave] = item
            if i % 50 == 0:
                log(f"  {i}/{len(empresas)} empresas")
    return entregas


def sync_incremental(ini, fim, entregas, desde):
    alteradas = paginar_entregas("deliveries/ListAll", DtInitial=ini.isoformat(), DtFinal=fim.isoformat(),
                                 DtLastDH=desde.strftime("%Y-%m-%d %H:%M:%S"), config="")
    for emp, e in alteradas:
        chave, item = converter(emp, e)
        entregas[chave] = item
    log(f"Incremental desde {desde:%d/%m %H:%M}: {len(alteradas)} entregas alteradas.")
    return entregas


def carregar(caminho):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anterior", help="dados.json da rodada anterior (para o incremental)")
    ap.add_argument("--saida", default="dados.json")
    ap.add_argument("--completo", action="store_true", help="força a busca completa")
    a = ap.parse_args()

    inicio = datetime.now().replace(microsecond=0)
    ini, fim = limites_do_mes(inicio.date())
    mes = ini.strftime("%Y-%m")
    ontem = datetime.combine(inicio.date() - timedelta(days=1), datetime.min.time())

    ant = carregar(a.anterior) if a.anterior else None
    precisa_completo = (
        a.completo
        or not ant
        or ant.get("versao") != VERSAO
        or ant.get("mes") != mes
        or datetime.fromisoformat(ant["completo_em"]) < inicio - INTERVALO_COMPLETO
        # a API só aceita ontem ou hoje no DtLastDH do ListAll
        or datetime.fromisoformat(ant["atualizado_em"]) < ontem
    )
    if precisa_completo:
        entregas = sync_completo(ini, fim)
        completo_em = inicio
    else:
        desde = max(datetime.fromisoformat(ant["atualizado_em"]) - timedelta(minutes=5), ontem)
        entregas = sync_incremental(ini, fim, ant["entregas"], desde)
        completo_em = datetime.fromisoformat(ant["completo_em"])

    dados = {
        "fonte": "Acessórias",
        "versao": VERSAO,
        "mes": mes,
        "atualizado_em": inicio.isoformat(),
        "completo_em": completo_em.isoformat(),
        "entregas": entregas,
    }
    Path(a.saida).write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"{a.saida}: {len(entregas)} entregas de {mes}.")


if __name__ == "__main__":
    main()
