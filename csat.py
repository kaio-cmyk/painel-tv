"""Gera o csat.json do Painel TV juntando as avaliações dos clientes de dois canais:
  - Bradial (WhatsApp): pesquisa de satisfação ao fim do atendimento;
  - Acessórias: nota que o cliente dá ao finalizar uma solicitação.
As duas usam notas de 1 a 5 e entram somadas nos indicadores.

Roda no GitHub Actions junto com o sincronizar.py (.github/workflows/painel.yml) e também localmente:
  python csat.py [--saida csat.json]

Mês atual (até hoje) e mês anterior inteiro. O arquivo publicado leva números, nota, comentário
(como o cliente escreveu, com o nome do colaborador se ele citou), data e canal. Nada de nome ou
telefone do cliente, empresa, nem número de conversa ou de solicitação. Se um canal falhar, o
arquivo sai só com o outro (campo "canais" diz quais entraram).

Chaves: BRADIAL_API_KEY e ACESSORIAS_TOKEN; localmente, caem para
.claude/integracoes/bradial/_credenciais.md e para o .mcp.json da raiz.
Datas no horário de Brasília (no Actions, TZ=America/Sao_Paulo).
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
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(__file__).resolve().parent.parent
VERSAO = 3
MAX_COMENTARIOS = 6
MAX_TEXTO = 160

BRADIAL = "https://api.bradial.com.br"
CSAT = "chat/v1/reports/csat"
ACESSORIAS = "https://api.acessorias.com"
# Limite do Acessórias é 100/min, dividido com o sincronizar.py, o MCP e o P.I.V.O.
PAUSA_ACESSORIAS = 0.8
# Trâmite gravado quando o cliente avalia: "Avaliou com nota: '5'" (e o que vier depois é comentário).
RE_AVALIACAO = re.compile(r"Avaliou com nota:\s*'?(\d)'?(.*)", re.S)


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def chave_bradial():
    if os.environ.get("BRADIAL_API_KEY"):
        return os.environ["BRADIAL_API_KEY"].strip()
    cred = RAIZ / ".claude" / "integracoes" / "bradial" / "_credenciais.md"
    if cred.exists():
        m = re.search(r"API Key\s*\|\s*`([^`]+)`", cred.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise RuntimeError("Defina a variável BRADIAL_API_KEY.")


def token_acessorias():
    if os.environ.get("ACESSORIAS_TOKEN"):
        return os.environ["ACESSORIAS_TOKEN"].strip()
    mcp = RAIZ / ".mcp.json"
    if mcp.exists():
        cfg = json.loads(mcp.read_text(encoding="utf-8"))
        return cfg["mcpServers"]["acessorias"]["headers"]["Authorization"].split()[-1]
    raise RuntimeError("Defina a variável ACESSORIAS_TOKEN.")


def chamar(url, headers, pausa=0):
    req = urllib.request.Request(url, headers=headers)
    for _ in range(4):
        time.sleep(pausa)
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
    raise RuntimeError(f"Sem resposta: {url.split('?')[0]}")


def texto_limpo(s):
    """Texto numa linha só; vazio se não tiver ao menos uma palavra (ex.: ".")."""
    s = " ".join((s or "").split())
    return s[:MAX_TEXTO] if re.search(r"[^\W\d_]{2}", s) else ""


# ---------------------------------------------------------------- Bradial (WhatsApp)

def bradial(ini, fim):
    """Avaliações do Bradial entre ini e fim e quantas pesquisas foram enviadas (pela taxa de resposta)."""
    headers = {"x-api-key": chave_bradial(),
               # Sem User-Agent próprio o Bradial devolve 403 (bloqueia o "Python-urllib" padrão).
               "User-Agent": "painel-tv-contajunto/1.0"}
    periodo = {"since": ini.isoformat(), "until": fim.isoformat()}

    lista, pagina = [], 1
    while pagina <= 500:  # vem de 5 em 5; o total está em "items"
        res = chamar(f"{BRADIAL}/{CSAT}/responses?{urllib.parse.urlencode({**periodo, 'page': pagina})}", headers)
        lote = (res or {}).get("data") or []
        lista.extend(lote)
        if not lote or len(lista) >= int(res.get("items") or 0):
            break
        pagina += 1

    avaliacoes = []
    for r in lista:
        if not r.get("rating"):
            continue
        try:
            quando = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except (KeyError, AttributeError, ValueError):
            quando = datetime.combine(ini, datetime.min.time())
        avaliacoes.append({"nota": int(r["rating"]), "texto": texto_limpo(r.get("feedbackMessage")),
                           "quando": quando, "canal": "WhatsApp"})

    enviadas = None
    try:
        res = chamar(f"{BRADIAL}/{CSAT}/overview?{urllib.parse.urlencode(periodo)}", headers)
        taxa = {k["id"]: k["value"] for k in res["kpis"]}.get("csatResponseRate")
        if isinstance(taxa, (int, float)) and taxa > 0:
            enviadas = round(len(avaliacoes) / taxa)
    except (RuntimeError, urllib.error.HTTPError, KeyError, TypeError) as e:
        log(f"Bradial sem taxa de resposta de {ini:%m/%Y}: {e}")
    return avaliacoes, enviadas


# ---------------------------------------------------------------- Acessórias (solicitações)

def data_br(s):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S")
    except (AttributeError, ValueError):
        return None


def acessorias(ini, fim):
    """Avaliações de solicitações externas do Acessórias de ini até fim, uma lista por avaliação,
    e as solicitações externas finalizadas (com a data) para calcular a taxa de resposta."""
    headers = {"Authorization": f"Bearer {token_acessorias()}"}

    def get(caminho, **query):
        return chamar(f"{ACESSORIAS}/{caminho}?{urllib.parse.urlencode(query)}", headers, PAUSA_ACESSORIAS)

    def listar(caminho, **query):
        itens, pagina = [], 1
        while pagina <= 200:
            res = get(caminho, **query, Pagina=pagina)
            if not isinstance(res, list) or not res:
                break
            itens.extend(res)
            pagina += 1
        return itens

    # A avaliação muda a data de última atualização, então quem avaliou no período está nesta lista.
    sols = [s for s in listar("requests/ListAll", SolUltAtIni=ini.isoformat(), SolUltAtFim=fim.isoformat())
            if s.get("SolTipo") == "Externa"]
    finalizadas = [d for d in (data_br(s.get("SolDHFinalizacao")) for s in sols) if d]

    # Nota dada por alguém do escritório não é avaliação de cliente.
    equipe = {(u.get("nome") or "").split()[0].lower() for u in listar("users/ListAll") if u.get("nome")}

    avaliacoes = []
    for s in sols:
        if not str(s.get("SolAvaliacao") or "").strip():
            continue
        det = get(f"requests/{s['SolID']}")
        det = det[0] if isinstance(det, list) and det else s
        clientes = {n.split()[0].lower() for n in det.get("SolEmpResp") or [] if n.strip()}
        tramites = [c for grupo in det.get("SolInteracoes") or [] for c in grupo]
        for c in tramites:
            m = RE_AVALIACAO.search(c.get("CmtText") or "")
            if not m:
                continue
            autor = (c.get("CmtUsuario") or "").strip().split()
            autor = autor[0].lower() if autor else ""
            if autor in equipe and autor not in clientes:
                log(f"Acessórias: avaliação da solicitação {s['SolID']} feita pela equipe; ignorada.")
                continue
            comentario = re.sub(r"^[\s'\"]*(Coment[aá]rio:)?\s*", "", m.group(2))
            avaliacoes.append({"nota": int(m.group(1)), "texto": texto_limpo(comentario),
                               "quando": data_br(c.get("CmtDH")) or data_br(s.get("SolDHUAt")),
                               "canal": "Acessórias"})
    return avaliacoes, finalizadas


# ---------------------------------------------------------------- resumo

def pct(parte, total):
    return round(parte * 100 / total) if total else None


def resumo(ini, fim, por_canal, enviadas, com_comentarios):
    """por_canal = {canal: [avaliações]}; enviadas = {canal: pesquisas enviadas ou None}."""
    todas = [a for lista in por_canal.values() for a in lista]
    notas = [a["nota"] for a in todas]
    # Taxa só com os canais que têm o total de pesquisas enviadas.
    com_total = [c for c in por_canal if enviadas.get(c)]
    resp = sum(len(por_canal[c]) for c in com_total)
    env = sum(enviadas[c] for c in com_total)
    out = {
        "periodo": [ini.isoformat(), fim.isoformat()],
        "respostas": len(notas),
        "satisfeitos_pct": pct(sum(n >= 4 for n in notas), len(notas)),
        "media": round(sum(notas) / len(notas), 2) if notas else None,
        "taxa_resposta": round(resp / env, 4) if env else None,
        "distribuicao": {str(i): notas.count(i) for i in range(1, 6)},
        "por_canal": {c: {"respostas": len(l),
                          "media": round(sum(a["nota"] for a in l) / len(l), 2) if l else None}
                      for c, l in por_canal.items()},
    }
    if com_comentarios:
        com_texto = sorted((a for a in todas if a["texto"]), key=lambda a: a["quando"], reverse=True)
        out["comentarios"] = [{"nota": a["nota"], "texto": a["texto"], "data": a["quando"].date().isoformat(),
                               "canal": a["canal"]} for a in com_texto[:MAX_COMENTARIOS]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="csat.json")
    a = ap.parse_args()

    agora = datetime.now().replace(microsecond=0)
    hoje = agora.date()
    ini_atual = hoje.replace(day=1)
    fim_anterior = ini_atual - timedelta(days=1)
    ini_anterior = fim_anterior.replace(day=1)
    meses = {"atual": (ini_atual, hoje), "anterior": (ini_anterior, fim_anterior)}
    no_mes = lambda d, mes: d is not None and meses[mes][0] <= d.date() <= meses[mes][1]

    por_canal = {m: {} for m in meses}
    enviadas = {m: {} for m in meses}

    try:
        for m, (ini, fim) in meses.items():
            por_canal[m]["WhatsApp"], enviadas[m]["WhatsApp"] = bradial(ini, fim)
    except (RuntimeError, urllib.error.HTTPError) as e:
        log(f"Bradial fora deste arquivo: {e}")
        for m in meses:
            por_canal[m].pop("WhatsApp", None)

    try:
        avs, finalizadas = acessorias(ini_anterior, hoje)
        for m in meses:
            por_canal[m]["Acessórias"] = [x for x in avs if no_mes(x["quando"], m)]
            enviadas[m]["Acessórias"] = sum(no_mes(d, m) for d in finalizadas)
    except (RuntimeError, urllib.error.HTTPError) as e:
        log(f"Acessórias fora deste arquivo: {e}")

    canais = list(por_canal["atual"])
    if not canais:
        sys.exit("Nenhum canal respondeu; csat.json não gerado.")

    dados = {
        "fonte": " + ".join("Bradial (WhatsApp)" if c == "WhatsApp" else c for c in canais),
        "canais": canais,
        "versao": VERSAO,
        "mes": ini_atual.strftime("%Y-%m"),
        "atualizado_em": agora.isoformat(),
        "atual": resumo(ini_atual, hoje, por_canal["atual"], enviadas["atual"], True),
        "anterior": resumo(ini_anterior, fim_anterior, por_canal["anterior"], enviadas["anterior"], False),
    }
    Path(a.saida).write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for m in meses:
        r = dados[m]
        canal = ", ".join(f"{c} {v['respostas']}" for c, v in r["por_canal"].items())
        log(f"{a.saida}: {m} {meses[m][0]:%m/%Y} = {r['respostas']} avaliações ({canal}), "
            f"média {r['media']}, taxa {r['taxa_resposta']}.")


if __name__ == "__main__":
    main()
