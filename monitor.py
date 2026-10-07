"""
Monitor do concurso TJPR (FGV) - versão 2.

Detecta qualquer movimentação na seção "Arquivos do concurso":
  - item novo (com ou sem link, inclusive com link igual a um item antigo);
  - item alterado ou removido;
  - arquivo PDF substituído no mesmo endereço (verifica os mais recentes);
  - mudança no cabeçalho da página (ex.: status "Em Andamento").
Evita cache do site com parâmetro aleatório e cabeçalhos no-cache.
"""
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString

URL = "https://conhecimento.fgv.br/concursos/tjpr25"
ARQUIVO_ESTADO = Path("estado.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MODO_TESTE = os.environ.get("TESTE", "false").lower() == "true"
QTD_PDFS_VERIFICADOS = 15  # quantos PDFs mais recentes checar por substituição

DATA_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
DATA_INICIO_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}


def limpar(t: str) -> str:
    return " ".join(t.split())


def notificar(titulo: str, mensagem: str, link: str = URL) -> None:
    if not NTFY_TOPIC:
        print(f"[sem NTFY_TOPIC] {titulo}: {mensagem}")
        return
    r = requests.post(
        "https://ntfy.sh",
        json={
            "topic": NTFY_TOPIC,
            "title": titulo,
            "message": mensagem[:3500],
            "priority": 5,
            "tags": ["rotating_light"],
            "click": link,
        },
        timeout=30,
    )
    r.raise_for_status()
    print(f"Notificação enviada: {titulo} | {mensagem[:80]}")


def baixar_pagina() -> str:
    # Duas tentativas, cada uma com parâmetro aleatório para furar o cache
    ultimo_erro = None
    for _ in range(2):
        try:
            r = requests.get(
                URL, params={"nocache": uuid.uuid4().hex},
                headers=HEADERS, timeout=60,
            )
            r.raise_for_status()
            return r.text
        except Exception as e:
            ultimo_erro = e
            time.sleep(10)
    raise RuntimeError(f"Não foi possível baixar a página: {ultimo_erro}")


def analisar(html: str) -> dict:
    """Divide a seção em blocos, cada um iniciado por uma data."""
    soup = BeautifulSoup(html, "html.parser")

    inicio = next(
        (h for h in soup.find_all(["h1", "h2", "h3", "h4"])
         if "arquivos do concurso" in h.get_text(" ", strip=True).lower()),
        None,
    )
    if inicio is None:
        raise RuntimeError("Seção 'Arquivos do concurso' não encontrada.")

    # Cabeçalho = status + título + texto introdutório (do <h1> até a seção de arquivos)
    cabecalho = []
    h1 = soup.find("h1")
    if h1:
        anterior = h1.find_previous(string=lambda s: limpar(str(s)) in ("Em Andamento", "Encerrado", "Suspenso"))
        if anterior:
            cabecalho.append(limpar(str(anterior)))
        for el in h1.next_elements:
            if el is inicio:
                break
            if isinstance(el, NavigableString) and not el.find_parent(["script", "style", "noscript"]):
                txt = limpar(str(el))
                if txt:
                    cabecalho.append(txt)

    blocos, atual = [], None
    for el in inicio.next_elements:
        nome = getattr(el, "name", None)
        if nome in ("h2", "h3", "h4") and "receba" in el.get_text(" ", strip=True).lower():
            break
        if isinstance(el, NavigableString):
            if el.find_parent(["script", "style", "noscript"]):
                continue
            txt = limpar(str(el))
            if not txt or (el.parent is inicio):
                continue
            m = DATA_INICIO_RE.match(txt)
            if DATA_RE.match(txt) or m:
                data = m.group(1) if m else txt
                atual = {"data": data, "textos": [], "links": []}
                blocos.append(atual)
                if m:
                    atual["textos"].append(m.group(2))
            else:
                if atual is None:  # item sem data no topo da lista
                    atual = {"data": "(sem data)", "textos": [], "links": []}
                    blocos.append(atual)
                atual["textos"].append(txt)
        elif nome == "a" and el.get("href") and atual is not None:
            atual["links"].append(requests.compat.urljoin(URL, el["href"]))

    if not blocos:
        raise RuntimeError("Nenhum item encontrado na seção.")

    itens = []
    for b in blocos:
        texto = " | ".join(b["textos"]) or "(item sem texto)"
        chave = hashlib.sha256(
            json.dumps([b["data"], texto, b["links"]], ensure_ascii=False).encode()
        ).hexdigest()
        itens.append({"data": b["data"], "texto": texto, "links": b["links"], "chave": chave})

    return {"cabecalho": " ".join(cabecalho), "itens": itens}


def assinaturas_pdfs(itens: list[dict]) -> dict:
    """ETag/Last-Modified/tamanho dos PDFs mais recentes, para detectar substituição."""
    pdfs = []
    for it in itens:
        for l in it["links"]:
            if l.lower().endswith(".pdf") and l not in pdfs:
                pdfs.append(l)
    assin = {}
    for l in pdfs[:QTD_PDFS_VERIFICADOS]:
        try:
            r = requests.head(l, headers=HEADERS, timeout=30, allow_redirects=True)
            if r.ok:
                assin[l] = "|".join(
                    r.headers.get(h, "") for h in ("ETag", "Last-Modified", "Content-Length")
                )
        except Exception:
            pass
    return assin


def mais_recente(itens: list[dict]) -> dict:
    def k(i):
        d = i["data"]
        return d[6:] + d[3:5] + d[:2] if DATA_RE.match(d) else "0"
    return max(itens, key=k)


def main() -> None:
    if MODO_TESTE:
        notificar("Teste do monitor TJPR", "Se você ouviu o som, está tudo funcionando.")
        return

    try:
        pagina = analisar(baixar_pagina())
    except Exception as e:
        print(f"Falha: {e}")
        estado = json.loads(ARQUIVO_ESTADO.read_text("utf-8")) if ARQUIVO_ESTADO.exists() else {}
        falhas = estado.get("falhas_seguidas", 0) + 1
        estado["falhas_seguidas"] = falhas
        ARQUIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), "utf-8")
        if falhas == 6:  # ~1 hora sem conseguir ler a página
            notificar("Monitor TJPR com problema",
                      f"Não consigo ler a página há cerca de 1 hora. Último erro: {e}")
        return

    pdfs = assinaturas_pdfs(pagina["itens"])
    novo_estado = {
        "cabecalho": pagina["cabecalho"],
        "itens": {i["chave"]: f'{i["data"]} - {i["texto"]}' for i in pagina["itens"]},
        "pdfs": pdfs,
        "falhas_seguidas": 0,
    }

    if not ARQUIVO_ESTADO.exists() or "itens" not in json.loads(ARQUIVO_ESTADO.read_text("utf-8")):
        ARQUIVO_ESTADO.write_text(json.dumps(novo_estado, ensure_ascii=False, indent=1), "utf-8")
        mr = mais_recente(pagina["itens"])
        notificar("Monitor TJPR ativado",
                  f'{len(pagina["itens"])} itens registrados. Mais recente: '
                  f'{mr["data"]} - {mr["texto"]}')
        return

    antigo = json.loads(ARQUIVO_ESTADO.read_text("utf-8"))
    avisos = []

    for it in pagina["itens"]:
        if it["chave"] not in antigo["itens"]:
            avisos.append(("Nova movimentação no concurso TJPR",
                           f'{it["data"]} - {it["texto"]}',
                           it["links"][0] if it["links"] else URL))

    removidos = set(antigo["itens"]) - set(novo_estado["itens"])
    if removidos and len(removidos) <= 5:
        for k in removidos:
            avisos.append(("Item alterado/removido no concurso TJPR", antigo["itens"][k], URL))
    elif removidos:
        avisos.append(("Concurso TJPR: página reorganizada",
                       f"{len(removidos)} itens mudaram. Confira o site.", URL))

    for link, assin in pdfs.items():
        anterior = antigo.get("pdfs", {}).get(link)
        if anterior and assin and anterior != assin:
            avisos.append(("Arquivo substituído no concurso TJPR",
                           f"O documento foi atualizado no mesmo endereço: {link}", link))

    if antigo.get("cabecalho") and antigo["cabecalho"] != pagina["cabecalho"]:
        avisos.append(("Cabeçalho do concurso TJPR mudou",
                       pagina["cabecalho"][:300], URL))

    for titulo, msg, link in avisos[:8]:
        notificar(titulo, msg, link)
    if len(avisos) > 8:
        notificar("Concurso TJPR", f"E mais {len(avisos) - 8} alterações. Confira o site.")
    if not avisos:
        mr = mais_recente(pagina["itens"])
        print(f'Nenhuma novidade. Mais recente: {mr["data"]} - {mr["texto"][:80]}')

    ARQUIVO_ESTADO.write_text(json.dumps(novo_estado, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
