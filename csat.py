"""Gera o csat.json do Painel TV a partir das pesquisas de satisfação do Bradial (WhatsApp).

Roda no GitHub Actions junto com o sincronizar.py (.github/workflows/painel.yml) e também localmente:
  python csat.py [--saida csat.json]

Mês atual (até hoje) e mês anterior inteiro. O arquivo publicado leva só números, nota, comentário
e data: nenhum nome de colaborador (nem por atendente, nem dentro do comentário), nada de nome ou
telefone do cliente, nem o número da conversa.

Chave: variável BRADIAL_API_KEY; localmente, cai para .claude/integracoes/bradial/_credenciais.md.
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
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.bradial.com.br"
CSAT = "chat/v1/reports/csat"
VERSAO = 2
# Nome de colaborador citado no comentário vira isto.
MASCARA = "[atendente]"
# Partes do nome que não identificam ninguém sozinhas.
IGNORAR_NOME = {"de", "da", "do", "das", "dos", "e"}
MAX_COMENTARIOS = 6
MAX_TEXTO = 160


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def chave():
    if os.environ.get("BRADIAL_API_KEY"):
        return os.environ["BRADIAL_API_KEY"].strip()
    cred = Path(__file__).resolve().parent.parent / ".claude" / "integracoes" / "bradial" / "_credenciais.md"
    if cred.exists():
        m = re.search(r"API Key\s*\|\s*`([^`]+)`", cred.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    sys.exit("Defina a variável BRADIAL_API_KEY.")


CHAVE = chave()


def chamar(caminho, **query):
    url = f"{BASE}/{caminho}?{urllib.parse.urlencode(query)}"
    # Sem User-Agent próprio o Bradial devolve 403 (bloqueia o "Python-urllib" padrão).
    req = urllib.request.Request(url, headers={"x-api-key": CHAVE, "User-Agent": "painel-tv-contajunto/1.0"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log("Limite de requisições atingido; aguardando 60 s.")
                time.sleep(60)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10)
    raise RuntimeError(f"Bradial não respondeu: {caminho}")


def paginar(caminho, **query):
    """Segue as páginas até completar o total informado em 'items' (CSAT vem de 5 em 5)."""
    itens, pagina = [], 1
    while pagina <= 500:
        res = chamar(caminho, **query, page=pagina)
        lote = res.get("data") or []
        itens.extend(lote)
        if not lote or len(itens) >= int(res.get("items") or 0):
            break
        pagina += 1
    return itens


def respostas(ini, fim):
    return paginar(f"{CSAT}/responses", since=ini.isoformat(), until=fim.isoformat())


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def nomes_da_equipe():
    """Cada parte do nome dos usuários do Bradial (sem acento, minúsculo), para mascarar nos comentários."""
    nomes = set()
    for ag in paginar("v2/public-api/v1/agents"):
        for parte in re.findall(r"\w+", ag.get("name") or ""):
            p = sem_acento(parte)
            if len(p) >= 3 and p not in IGNORAR_NOME:
                nomes.add(p)
    return nomes


def mascarar(texto, nomes):
    texto = re.sub(r"\w+", lambda m: MASCARA if sem_acento(m.group()) in nomes else m.group(), texto)
    # "Tatiane Faria" vira um [atendente] só
    return re.sub(rf"{re.escape(MASCARA)}(\s+{re.escape(MASCARA)})+", MASCARA, texto)


def dia_local(iso):
    """createdAt vem em UTC; converte para o dia no horário local (Brasília)."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (AttributeError, ValueError):
        return None


def pct(parte, total):
    return round(parte * 100 / total) if total else None


def media(notas):
    return round(sum(notas) / len(notas), 2) if notas else None


def resumo(ini, fim, nomes=None):
    """nomes = partes do nome da equipe; só o mês atual leva comentários (None = sem comentários)."""
    lista = respostas(ini, fim)
    taxa = None
    try:
        kpis = {k["id"]: k["value"] for k in chamar(f"{CSAT}/overview", since=ini.isoformat(), until=fim.isoformat())["kpis"]}
        taxa = kpis.get("csatResponseRate")
    except (RuntimeError, urllib.error.HTTPError, KeyError, TypeError) as e:
        log(f"Sem taxa de resposta de {ini:%m/%Y}: {e}")

    notas = [int(r["rating"]) for r in lista if r.get("rating")]
    out = {
        "periodo": [ini.isoformat(), fim.isoformat()],
        "respostas": len(notas),
        "satisfeitos_pct": pct(sum(n >= 4 for n in notas), len(notas)),
        "media": media(notas),
        "taxa_resposta": round(taxa, 4) if isinstance(taxa, (int, float)) else None,
        "distribuicao": {str(i): notas.count(i) for i in range(1, 6)},
    }
    if nomes is not None:
        # Só nota, texto e dia. Nome de colaborador citado no texto é mascarado.
        com_texto = [r for r in lista if (r.get("feedbackMessage") or "").strip() and r.get("rating")]
        com_texto.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        out["comentarios"] = [
            {"nota": int(r["rating"]),
             "texto": mascarar(" ".join(r["feedbackMessage"].split()), nomes)[:MAX_TEXTO],
             "data": dia_local(r.get("createdAt"))}
            for r in com_texto[:MAX_COMENTARIOS]
        ]
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

    try:
        nomes = nomes_da_equipe()
    except (RuntimeError, urllib.error.HTTPError) as e:
        nomes = None
        log(f"Sem a lista da equipe ({e}): comentários não serão publicados.")
    if not nomes:
        nomes = None  # sem como mascarar nomes, não publica comentário nenhum

    atual = resumo(ini_atual, hoje, nomes)
    atual.setdefault("comentarios", [])
    dados = {
        "fonte": "Bradial",
        "versao": VERSAO,
        "mes": ini_atual.strftime("%Y-%m"),
        "atualizado_em": agora.isoformat(),
        "atual": atual,
        "anterior": resumo(ini_anterior, fim_anterior),
    }
    Path(a.saida).write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"{a.saida}: {dados['atual']['respostas']} respostas em {ini_atual:%m/%Y}, "
        f"{dados['anterior']['respostas']} em {ini_anterior:%m/%Y}.")


if __name__ == "__main__":
    main()
