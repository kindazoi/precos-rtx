#!/usr/bin/env python3
"""Rastreador diário de preços das RTX 5070 Ti na Globaldata e na Novo Atalho.

Cada execução:
  1. abre as páginas das lojas num browser headless (Playwright);
  2. extrai nome, preço, disponibilidade e link de cada placa;
  3. junta tudo ao histórico em data/precos.csv;
  4. gera o relatório docs/index.html (lista + gráfico por placa);
  5. envia alerta por email e/ou WhatsApp se algum preço baixou.
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
LOJAS = {
    "Globaldata": os.getenv(
        "GLOBALDATA_URL",
        "https://www.globaldata.pt/componentes/placas-graficas/placas-graficas-nvidia/geforce-rtx-5070-ti?sz=96",
    ),
    # Confirma este endereço: pesquisa "rtx 5070 ti" no site e copia o URL da página de resultados.
    "Novo Atalho": os.getenv("NOVOATALHO_URL", "https://www.novoatalho.pt/pesquisa?s=rtx+5070+ti"),
}
PRECO_MINIMO = 300.0  # ignora valores tipo "poupa 50 €" ou portes
EXCLUIR = re.compile(r"port[áa]til|laptop|notebook|computador|desktop|\bpc\b|mini.?pc|bundle|pack", re.I)
DADOS = Path("data/precos.csv")
RELATORIO = Path("docs/index.html")
MODELO_HTML = Path(__file__).with_name("relatorio_modelo.html")
CAMPOS = ["data", "loja", "url", "nome", "preco", "disponivel"]

# Corre dentro da página: encontra links com "5070 Ti", sobe até ao "cartão" do produto
# (o maior elemento que só contém esse produto) e lê os preços e o texto de stock.
EXTRAIR_JS = r"""
() => {
  const alvo = /5070\s*-?\s*ti/i;
  const precoRe = /(\d{1,3}(?:[.\s\u00a0]\d{3})*|\d+),(\d{2})\s*€|€\s*(\d{1,3}(?:[.\s\u00a0]\d{3})*|\d+),(\d{2})/g;
  const limpa = h => h.split('#')[0].split('?')[0];
  const nomeDe = a => (a.getAttribute('title') || a.innerText || a.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim();
  const prod = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const nome = nomeDe(a);
    if (!alvo.test(nome)) continue;
    const url = limpa(a.href);
    const p = prod.get(url) || { url, nome: '', texto: '' };
    if (nome.length > p.nome.length) p.nome = nome;
    let el = a;
    for (let i = 0; i < 10; i++) {
      const pai = el.parentElement;
      if (!pai) break;
      const urls = new Set([...pai.querySelectorAll('a[href]')]
        .filter(x => alvo.test(nomeDe(x))).map(x => limpa(x.href)));
      if (urls.size > 1) break;
      el = pai;
    }
    const texto = el.innerText || '';
    if (texto.length > p.texto.length) p.texto = texto;
    prod.set(url, p);
  }
  return [...prod.values()].map(p => ({
    url: p.url,
    nome: p.nome,
    precos: [...p.texto.matchAll(precoRe)].map(m => (m[1] || m[3]) + ',' + (m[2] || m[4])),
    esgotado: /esgotad|sem stock|indispon[íi]vel|fora de stock/i.test(p.texto),
  }));
}
"""


# ---------------------------------------------------------------- recolha
def para_float(s: str) -> float:
    return float(re.sub(r"[\s\u00a0.]", "", s).replace(",", "."))


def normalizar(loja, brutos):
    itens = []
    for b in brutos:
        if EXCLUIR.search(b["nome"]):
            continue
        precos = [v for v in map(para_float, b["precos"]) if v >= PRECO_MINIMO]
        if not precos:
            continue
        itens.append({
            "loja": loja,
            "url": b["url"],
            "nome": b["nome"][:160],
            "preco": min(precos),  # com desconto, o preço riscado é o maior
            "disponivel": not b["esgotado"],
        })
    return itens


async def recolher():
    from playwright.async_api import async_playwright

    resultados, falhas = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            locale="pt-PT",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        for loja, url in LOJAS.items():
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(4000)
                for _ in range(6):  # força o carregamento de produtos com lazy loading
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(800)
                brutos = await page.evaluate(EXTRAIR_JS)
            except Exception as e:  # noqa: BLE001
                print(f"[{loja}] erro ao abrir {url}: {e}")
                falhas.append(loja)
                continue
            finally:
                await page.close()
            itens = normalizar(loja, brutos)
            print(f"[{loja}] {len(itens)} placas encontradas")
            if not itens:
                falhas.append(loja)
            resultados.extend(itens)
        await browser.close()
    return resultados, falhas


# ---------------------------------------------------------------- histórico
def ler_historico():
    if not DADOS.exists():
        return []
    with DADOS.open(newline="", encoding="utf-8") as f:
        return [{**r, "preco": float(r["preco"]), "disponivel": r["disponivel"] == "1"}
                for r in csv.DictReader(f)]


def gravar_historico(hist):
    DADOS.parent.mkdir(parents=True, exist_ok=True)
    hist = sorted(hist, key=lambda r: (r["data"], r["loja"], r["nome"]))
    with DADOS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for r in hist:
            w.writerow({**{k: r[k] for k in CAMPOS}, "preco": f'{r["preco"]:.2f}',
                        "disponivel": "1" if r["disponivel"] else "0"})


def detetar_descidas(hist, hoje, atuais):
    descidas = []
    for a in atuais:
        if not a["disponivel"]:
            continue
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
        s = series.setdefault(r["url"], {"url": r["url"], "loja": r["loja"], "nome": r["nome"], "pontos": []})
        s["nome"] = r["nome"]
        s["pontos"].append({"data": r["data"], "preco": r["preco"], "disponivel": r["disponivel"]})
    dados = {"hoje": hoje, "series": list(series.values())}
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
    linhas = [f'{d["nome"]} ({d["loja"]}): {eur(d["anterior"])} → {eur(d["preco"])} '
              f'(-{eur(d["anterior"] - d["preco"])})\n{d["url"]}' for d in descidas]
    texto = "Baixaram de preço:\n\n" + "\n\n".join(linhas) + (f"\n\nRelatório: {link}" if link else "")
    itens = "".join(
        f'<li><a href="{d["url"]}">{d["nome"]}</a> ({d["loja"]})<br>'
        f'<s>{eur(d["anterior"])}</s> → <b>{eur(d["preco"])}</b></li>' for d in descidas)
    corpo = f"<p>Baixaram de preço:</p><ul>{itens}</ul>" + (f'<p><a href="{link}">Ver relatório</a></p>' if link else "")
    assunto = f"RTX 5070 Ti: {len(descidas)} preço(s) baixaram"
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
    atuais, falhas = asyncio.run(recolher())
    hist = ler_historico()
    descidas = detetar_descidas(hist, hoje, atuais)

    lojas_ok = {a["loja"] for a in atuais}
    hist = [r for r in hist if not (r["data"] == hoje and r["loja"] in lojas_ok)]
    hist += [{**a, "data": hoje} for a in atuais]
    gravar_historico(hist)
    gerar_relatorio(hist, hoje)

    for a in sorted((a for a in atuais if a["disponivel"]), key=lambda a: -a["preco"]):
        print(f'{eur(a["preco"]):>12}  {a["loja"]:<12} {a["nome"]}')

    if descidas and os.getenv("SEM_ALERTAS") != "1":
        alertar(descidas)
    if falhas:
        print(f"Sem resultados em: {', '.join(falhas)}. Verifica o URL ou se o site mudou.")
        sys.exit(1)


if __name__ == "__main__":
    main()
