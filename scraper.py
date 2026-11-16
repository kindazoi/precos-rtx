#!/usr/bin/env python3
"""Rastreador diário de preços na Globaldata.

Cada execução:
  1. abre a página de cada produto seguido num browser headless (Playwright);
  2. extrai nome, preço, estado (em stock, esgotado, ...) e link de cada variante;
  3. junta tudo ao histórico em data/precos.csv;
  4. gera o relatório docs/index.html (uma secção por produto, com tabela e gráficos);
  5. envia alerta por email e/ou WhatsApp se algum preço baixou.

Para seguir outro produto, acrescenta uma entrada à lista PRODUTOS.
"""
import asyncio
import csv
import json
import os
import re
import smtplib
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import requests

# ---------------------------------------------------------------- configuração
# Para cada produto:
#   url      página da Globaldata onde ele aparece (categoria ou pesquisa; "sz=96" mostra 96 por página)
#   incluir  o nome do artigo tem de conter isto
#   excluir  artigos cujo nome contenha isto são ignorados (portáteis, acessórios, ...)
#   minimo   preços abaixo deste valor são ignorados (ex.: "poupa 50 €")
ACESSORIOS = r"skate|grip|dock|cabo|capa|bolsa|teclas|substitui|kit|bundle|pack"
PRODUTOS = [
    {
        "produto": "RTX 5070 Ti",
        "url": "https://www.globaldata.pt/componentes/placas-graficas/placas-graficas-nvidia/geforce-rtx-5070-ti?sz=96",
        "incluir": r"5070\s*-?\s*ti",
        "excluir": r"port[áa]til|laptop|notebook|computador|desktop|\bpc\b|mini.?pc|bundle|pack|ryzen|core i\d"
                   r"|core ultra|\bssd\b|\bram\b|\bddr[45]\b|windows|\d{2}(\.\d)?\"",
        "minimo": 300,
    },
    {
        "produto": "Razer Naga V3 Pro",
        "url": "https://www.globaldata.pt/search?q=razer+naga+v3+pro&srule=relevancia&sz=96",
        "incluir": r"naga\s*v3\s*pro",
        "excluir": ACESSORIOS,
        "minimo": 40,
    },
    {
        "produto": "Corsair Scimitar Elite Wireless",
        "url": "https://www.globaldata.pt/search?q=corsair+scimitar+elite+wireless&srule=relevancia&sz=96",
        "incluir": r"scimitar.{0,20}elite.{0,30}wireless|scimitar.{0,20}wireless.{0,30}elite",
        "excluir": ACESSORIOS,
        "minimo": 40,
    },
]

# Estado de cada artigo, lido do texto do cartão do produto. A primeira regra que bater ganha.
ESTADOS = [
    ("Esgotado", r"esgotad|sem stock|fora de stock|indispon[íi]vel"),
    ("Pré-venda", r"pr[ée][- ]?(venda|reserva)"),
    ("Por encomenda", r"por encomenda|sob consulta|\d+\s*(a\s*\d+\s*)?dias"),
    ("Em stock", r"em stock|dispon[íi]vel|previsto hoje|previsto amanh[ãa]|envio imediato"),
]
DADOS = Path("data/precos.csv")
RELATORIO = Path("docs/index.html")
MODELO_HTML = Path(__file__).with_name("relatorio_modelo.html")
CAMPOS = ["data", "produto", "loja", "url", "nome", "preco", "estado"]
LOJA = "Globaldata"

# Corre dentro da página: encontra links cujo nome bate com "alvo", sobe até ao "cartão" do produto
# (o maior elemento que só contém esse produto) e lê os preços e o texto de stock.
EXTRAIR_JS = r"""
(fonte) => {
  const alvo = new RegExp(fonte, 'i');
  const precoRe = /(\d{1,3}(?:[.\s  ]\d{3})*|\d+)[,.](\d{2})\s*€|€\s*(\d{1,3}(?:[.\s  ]\d{3})*|\d+)[,.](\d{2})/g;
  const limpa = h => h.split('#')[0].split('?')[0];
  const nomeDe = a => {
    const cands = [a.getAttribute('title'), a.getAttribute('aria-label'), a.innerText, a.textContent,
                   ...[...a.querySelectorAll('img[alt]')].map(i => i.alt)]
      .filter(Boolean).map(t => t.replace(/\s+/g, ' ').trim());
    return cands.filter(t => alvo.test(t)).sort((x, y) => y.length - x.length)[0] || '';
  };
  const prod = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const nome = nomeDe(a);
    if (!nome) continue;
    const url = limpa(a.href);
    const p = prod.get(url) || { url, nome: '', texto: '' };
    if (nome.length > p.nome.length) p.nome = nome;
    let el = a;
    for (let i = 0; i < 10; i++) {
      const pai = el.parentElement;
      if (!pai) break;
      const urls = new Set([...pai.querySelectorAll('a[href]')]
        .filter(x => nomeDe(x)).map(x => limpa(x.href)));
      if (urls.size > 1) break;
      el = pai;
    }
    const texto = el.innerText || el.textContent || '';
    if (texto.length > p.texto.length) p.texto = texto;
    prod.set(url, p);
  }
  return [...prod.values()].map(p => ({
    url: p.url,
    nome: p.nome,
    precos: [...p.texto.matchAll(precoRe)].map(m => (m[1] || m[3]) + ',' + (m[2] || m[4])),
    texto: p.texto.slice(0, 2000),
  }));
}
"""


# ---------------------------------------------------------------- recolha
def para_float(s: str) -> float:
    inteiro, dec = s.rsplit(",", 1)
    return float(re.sub(r"\D", "", inteiro) + "." + dec)


def ler_estado(texto: str) -> str:
    for estado, padrao in ESTADOS:
        if re.search(padrao, texto, re.I):
            return estado
    return "Desconhecido"


def normalizar(cfg, brutos):
    itens = []
    for b in brutos:
        if re.search(cfg["excluir"], b["nome"], re.I):
            continue
        precos = [v for v in map(para_float, b["precos"]) if v >= cfg["minimo"]]
        if not precos:
            continue
        itens.append({
            "produto": cfg["produto"],
            "loja": LOJA,
            "url": b["url"],
            "nome": b["nome"][:160],
            "preco": min(precos),  # com desconto, o preço riscado é o maior
            "estado": ler_estado(b["texto"]),
        })
    return itens


async def diagnosticar(page, cfg, brutos):
    """Guarda uma captura e o HTML da página em debug/ e mostra pistas no registo."""
    pasta = Path("debug"); pasta.mkdir(exist_ok=True)
    nome = re.sub(r"\W+", "_", cfg["produto"].lower())
    try:
        await page.screenshot(path=str(pasta / f"{nome}.png"), full_page=True)
        (pasta / f"{nome}.html").write_text(await page.content(), encoding="utf-8")
        texto = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception as e:  # noqa: BLE001
        print(f"    diagnóstico falhou: {e}")
        return
    print(f"    título da página: {await page.title()!r}")
    print(f"    endereço final:   {page.url}")
    print(f"    artigos lidos antes dos filtros: {len(brutos)}")
    for b in brutos[:5]:
        print(f"      - {b['nome'][:80]!r}  preços={b['precos'][:3]}")
    print(f"    início do texto da página: {texto[:300]!r}")


async def recolher():
    from playwright.async_api import async_playwright

    resultados, vazios = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            locale="pt-PT",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        for cfg in PRODUTOS:
            page = await ctx.new_page()
            try:
                await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60_000)
                try:  # espera até aparecer o produto (resultados carregados por JavaScript)
                    await page.wait_for_function(
                        "(f) => new RegExp(f, 'i').test(document.body ? document.body.innerText : '')",
                        arg=cfg["incluir"], timeout=25_000)
                except Exception:  # noqa: BLE001
                    pass
                await page.wait_for_timeout(2000)
                for _ in range(6):  # força o carregamento de produtos com lazy loading
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(800)
                brutos = await page.evaluate(EXTRAIR_JS, cfg["incluir"])
                itens = normalizar(cfg, brutos)
                print(f"[{cfg['produto']}] {len(itens)} artigos encontrados")
                if not itens:
                    print(f"[{cfg['produto']}] AVISO: nada encontrado. Pode estar fora do catálogo ou o site mudou.")
                    await diagnosticar(page, cfg, brutos)
                    vazios.append(cfg["produto"])
                resultados.extend(itens)
            except Exception as e:  # noqa: BLE001
                print(f"[{cfg['produto']}] erro ao abrir {cfg['url']}: {e}")
                vazios.append(cfg["produto"])
            finally:
                await page.close()
        await browser.close()
    return resultados, vazios


# ---------------------------------------------------------------- histórico
def ler_historico():
    if not DADOS.exists():
        return []
    with DADOS.open(newline="", encoding="utf-8") as f:
        linhas = []
        for r in csv.DictReader(f):
            if r.get("loja") and r["loja"] != LOJA:
                continue  # ignora lojas que deixaram de ser seguidas
            # aceita formatos antigos: sem "produto" (só RTX) e com "disponivel" (1/0) em vez de "estado"
            estado = r.get("estado") or ("Em stock" if r.get("disponivel") == "1" else "Esgotado")
            linhas.append({"data": r["data"], "produto": r.get("produto") or "RTX 5070 Ti", "loja": LOJA,
                           "url": r["url"], "nome": r["nome"], "preco": float(r["preco"]), "estado": estado})
        return linhas


def gravar_historico(hist):
    DADOS.parent.mkdir(parents=True, exist_ok=True)
    hist = sorted(hist, key=lambda r: (r["data"], r["produto"], r["nome"]))
    with DADOS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for r in hist:
            w.writerow({**{k: r[k] for k in CAMPOS}, "preco": f'{r["preco"]:.2f}'})


def detetar_descidas(hist, hoje, atuais):
    descidas = []
    for a in atuais:
        anteriores = [r for r in hist if r["url"] == a["url"] and r["data"] < hoje]
        if not anteriores:
            continue
        ultimo = max(anteriores, key=lambda r: r["data"])
        if a["preco"] < ultimo["preco"] - 0.005:
            descidas.append({**a, "anterior": ultimo["preco"], "data_anterior": ultimo["data"]})
    return descidas


# ---------------------------------------------------------------- relatório
def gerar_relatorio(hist, hoje):
    series = {}
    for r in sorted(hist, key=lambda r: r["data"]):
        s = series.setdefault(r["url"], {"url": r["url"], "produto": r["produto"], "loja": r["loja"],
                                         "nome": r["nome"], "pontos": []})
        s["nome"] = r["nome"]
        s["pontos"].append({"data": r["data"], "preco": r["preco"], "estado": r["estado"]})
    dados = {"hoje": hoje, "produtos": [c["produto"] for c in PRODUTOS], "series": list(series.values())}
    js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    RELATORIO.parent.mkdir(parents=True, exist_ok=True)
    RELATORIO.write_text(MODELO_HTML.read_text(encoding="utf-8").replace("__DADOS__", js), encoding="utf-8")


# ---------------------------------------------------------------- alertas
def eur(v: float) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def enviar_email(assunto, texto, corpo_html):
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (user and pw):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = assunto, user, os.getenv("EMAIL_TO") or user
    msg.set_content(texto)
    msg.add_alternative(corpo_html, subtype="html")
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def enviar_whatsapp(texto):
    phone, key = os.getenv("WHATSAPP_PHONE"), os.getenv("WHATSAPP_APIKEY")
    if not (phone and key):
        return False
    r = requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": phone, "text": texto, "apikey": key}, timeout=30)
    r.raise_for_status()
    return True


def alertar(descidas):
    link = os.getenv("REPORT_URL", "")
    linhas = [f'{d["nome"]} ({d["estado"]}): {eur(d["anterior"])} → {eur(d["preco"])} '
              f'(-{eur(d["anterior"] - d["preco"])})\n{d["url"]}' for d in descidas]
    texto = "Baixaram de preço na Globaldata:\n\n" + "\n\n".join(linhas) + (f"\n\nRelatório: {link}" if link else "")
    itens = "".join(
        f'<li><a href="{d["url"]}">{d["nome"]}</a> ({d["estado"]})<br>'
        f'<s>{eur(d["anterior"])}</s> → <b>{eur(d["preco"])}</b></li>' for d in descidas)
    corpo = (f"<p>Baixaram de preço na Globaldata:</p><ul>{itens}</ul>"
             + (f'<p><a href="{link}">Ver relatório</a></p>' if link else ""))
    assunto = "Preço baixou: " + ", ".join(sorted({d["produto"] for d in descidas}))
    enviados = []
    for nome, fn in (("email", lambda: enviar_email(assunto, texto, corpo)),
                     ("WhatsApp", lambda: enviar_whatsapp(f"{assunto}\n\n{texto}"))):
        try:
            if fn():
                enviados.append(nome)
        except Exception as e:  # noqa: BLE001
            print(f"Falha ao enviar {nome}: {e}")
    print("Alerta enviado por: " + (", ".join(enviados) or "nenhum canal configurado"))


# ---------------------------------------------------------------- principal
def main():
    hoje = os.getenv("DATA_HOJE") or date.today().isoformat()
    atuais, vazios = asyncio.run(recolher())
    hist = ler_historico()
    descidas = detetar_descidas(hist, hoje, atuais)

    produtos_ok = {a["produto"] for a in atuais}
    hist = [r for r in hist if not (r["data"] == hoje and r["produto"] in produtos_ok)]
    hist += [{**a, "data": hoje} for a in atuais]
    gravar_historico(hist)
    gerar_relatorio(hist, hoje)

    for cfg in PRODUTOS:
        lista = sorted((a for a in atuais if a["produto"] == cfg["produto"]), key=lambda a: -a["preco"])
        if lista:
            print(f"\n{cfg['produto']}")
        for a in lista:
            print(f'{eur(a["preco"]):>12}  {a["estado"]:<14} {a["nome"]}')

    if descidas and os.getenv("SEM_ALERTAS") != "1":
        alertar(descidas)
    # Só falha (e o GitHub avisa-te por email) se não encontrou nada em produto nenhum:
    # quase de certeza o site mudou ou bloqueou o acesso. Um produto em falta é só um aviso.
    if vazios and len(vazios) == len(PRODUTOS):
        print("\nNada encontrado em nenhum produto. Vê o diagnóstico desta execução.")
        sys.exit(1)


if __name__ == "__main__":
    main()
