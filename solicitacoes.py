"""Gera o solicitacoes.json do Painel TV: solicitações do Acessórias em aberto e sem resposta nossa,
separadas em externas (clientes) e internas (equipe), por departamento e tempo de espera.

Roda no GitHub Actions junto com o sincronizar.py (.github/workflows/painel.yml) e também localmente:
  python solicitacoes.py [--saida solicitacoes.json]

"Sem resposta" = em aberto (Nova, Resolvendo ou Cliente) e a última mensagem não é nossa:
  - externa: o autor não é da equipe (ou é um contato do cliente);
  - interna: o autor não é um dos responsáveis da solicitação.
A espera conta da primeira mensagem da sequência final que não é nossa.

O arquivo publicado leva só contagens por departamento e tempo de espera: nada de cliente, assunto,
número da solicitação ou nome de colaborador. Departamento filho ("Contábil - Emilly") entra no pai.

Token: ACESSORIAS_TOKEN; localmente, cai para o .mcp.json da raiz. Datas no horário de Brasília.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(__file__).resolve().parent.parent
VERSAO = 1
ACESSORIAS = "https://api.acessorias.com"
# Limite do Acessórias é 100/min, dividido com o sincronizar.py, o csat.py, o MCP e o P.I.V.O.
PAUSA = 0.8
# SolStatus no filtro do requests/ListAll: A = Nova, P = Resolvendo, C = Cliente (F = Finalizada).
STATUS_ABERTOS = ("A", "P", "C")
TIPOS = {"Externa": "externas", "Interna": "internas"}


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def token():
    if os.environ.get("ACESSORIAS_TOKEN"):
        return os.environ["ACESSORIAS_TOKEN"].strip()
    mcp = RAIZ / ".mcp.json"
    if mcp.exists():
        cfg = json.loads(mcp.read_text(encoding="utf-8"))
        return cfg["mcpServers"]["acessorias"]["headers"]["Authorization"].split()[-1]
    sys.exit("Defina a variável ACESSORIAS_TOKEN.")


HEADERS = {"Authorization": f"Bearer {token()}"}


def chamar(caminho, **query):
    url = f"{ACESSORIAS}/{caminho}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers=HEADERS)
    for _ in range(4):
        time.sleep(PAUSA)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                texto = r.read().decode("utf-8")
            return json.loads(texto) if texto.strip() else None
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


def listar(caminho, **query):
    itens, pagina = [], 1
    while pagina <= 200:
        res = chamar(caminho, **query, Pagina=pagina)
        if not isinstance(res, list) or not res:
            break
        itens.extend(res)
        pagina += 1
    return itens


def primeiro_nome(nome):
    partes = (nome or "").strip().split()
    return partes[0].lower() if partes else ""


def data_br(s):
    try:
        return datetime.strptime((s or "").strip(), "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None


def depto_pai(nome):
    return re.split(r"\s+[-–]\s+", (nome or "").strip())[0] or "Sem departamento"


def esperando_desde(sol, equipe):
    """Data da primeira mensagem da sequência final que não é nossa; None se a última for nossa."""
    tramites = [c for grupo in sol.get("SolInteracoes") or [] for c in grupo]
    clientes = {primeiro_nome(n) for n in sol.get("SolEmpResp") or []}
    responsaveis = {primeiro_nome(n) for n in sol.get("SolOfficeResp") or []}

    def nossa(c):
        autor = primeiro_nome(c.get("CmtUsuario"))
        if sol.get("SolTipo") == "Interna":
            return autor in responsaveis
        return autor in equipe and autor not in clientes

    desde = None
    for c in reversed(tramites):
        if nossa(c):
            break
        desde = data_br(c.get("CmtDH")) or desde
    return desde


def novo_grupo():
    return {"abertas": 0, "sem_resposta": 0, "faixas": {"ate1": 0, "de1a3": 0, "mais3": 0}, "mais_antiga_dias": None}


def somar(g, horas):
    g["sem_resposta"] += 1
    g["faixas"]["ate1" if horas < 24 else "de1a3" if horas <= 72 else "mais3"] += 1
    dias = int(horas // 24)
    g["mais_antiga_dias"] = max(g["mais_antiga_dias"] or 0, dias)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="solicitacoes.json")
    a = ap.parse_args()
    agora = datetime.now().replace(microsecond=0)

    sols = {}
    for st in STATUS_ABERTOS:
        for s in listar("requests/ListAll", SolStatus=st):
            sols[s["SolID"]] = s
    equipe = {primeiro_nome(u.get("nome")) for u in listar("users/ListAll")} - {""}
    log(f"{len(sols)} solicitações em aberto; {len(equipe)} nomes na equipe.")

    saida = {chave: {**novo_grupo(), "por_depto": {}} for chave in TIPOS.values()}
    for resumo in sols.values():
        chave = TIPOS.get(resumo.get("SolTipo"))
        if not chave:
            continue
        det = chamar(f"requests/{resumo['SolID']}")
        sol = det[0] if isinstance(det, list) and det else resumo
        grupo = saida[chave]
        depto = grupo["por_depto"].setdefault(depto_pai(sol.get("DptoNome") or resumo.get("DptoNome")), novo_grupo())
        grupo["abertas"] += 1
        depto["abertas"] += 1
        desde = esperando_desde(sol, equipe)
        if desde:
            horas = max(0.0, (agora - desde).total_seconds() / 3600)
            somar(grupo, horas)
            somar(depto, horas)

    for grupo in saida.values():
        grupo["por_depto"] = sorted(({"depto": d, **g} for d, g in grupo["por_depto"].items()),
                                    key=lambda x: (-x["sem_resposta"], -(x["mais_antiga_dias"] or 0), -x["abertas"], x["depto"]))

    dados = {"fonte": "Acessórias", "versao": VERSAO, "atualizado_em": agora.isoformat(), **saida}
    Path(a.saida).write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for chave in TIPOS.values():
        g = saida[chave]
        log(f"{a.saida}: {chave} {g['sem_resposta']} sem resposta de {g['abertas']} abertas "
            f"(faixas {g['faixas']}, mais antiga {g['mais_antiga_dias']} dias).")


if __name__ == "__main__":
    main()
