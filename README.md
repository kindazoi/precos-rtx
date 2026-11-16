# Preços RTX 5070 Ti (Globaldata + Novo Atalho)

Todos os dias o GitHub Actions abre as duas lojas, guarda os preços em `data/precos.csv`,
gera o relatório `docs/index.html` e avisa-te por email e/ou WhatsApp quando um preço baixa.

## 1. Criar o repositório
1. Cria um repositório novo no GitHub e envia estes ficheiros (incluindo a pasta `.github`).
2. Em **Settings → Actions → General → Workflow permissions**, escolhe *Read and write permissions*.

## 2. Alertas por email (Gmail)
1. Ativa a verificação em 2 passos na conta Google.
2. Cria uma *palavra-passe de app* em https://myaccount.google.com/apppasswords.
3. Em **Settings → Secrets and variables → Actions → Secrets**, cria:
   - `SMTP_USER` – o teu endereço Gmail
   - `SMTP_PASS` – a palavra-passe de app (16 letras)
   - `EMAIL_TO` – (opcional) outro endereço para receber os alertas

## 3. Alertas por WhatsApp (opcional, CallMeBot)
1. Adiciona o número do CallMeBot aos contactos e envia-lhe a mensagem de ativação
   indicada em https://www.callmebot.com/blog/free-api-whatsapp-messages/.
2. Vais receber uma `apikey`. Cria os secrets:
   - `WHATSAPP_PHONE` – o teu número com indicativo, ex.: `+351912345678`
   - `WHATSAPP_APIKEY` – a chave recebida

Se configurares os dois, recebes nos dois.

## 4. Confirmar o URL da Novo Atalho
Pesquisa "rtx 5070 ti" no site da Novo Atalho e copia o endereço da página de resultados.
Se for diferente de `https://www.novoatalho.pt/pesquisa?s=rtx+5070+ti`, cria em
**Settings → Secrets and variables → Actions → Variables** a variável `NOVOATALHO_URL` com esse endereço.

## 5. Ver o relatório (opcional)
Em **Settings → Pages**, escolhe *Deploy from a branch*, branch `main`, pasta `/docs`.
O relatório fica em `https://<utilizador>.github.io/<repositorio>/`. Coloca esse endereço na
variável `REPORT_URL` para ele aparecer nos alertas. (O Pages gratuito exige repositório público;
os secrets continuam privados.)

## 6. Primeiro teste
No separador **Actions**, abre *Preços RTX 5070 Ti* e carrega em **Run workflow**.
O registo mostra quantas placas foram encontradas em cada loja. Se uma loja der 0 resultados,
a execução fica a vermelho e o GitHub envia-te um email.

## Testar no teu computador
```
pip install -r requirements.txt
python -m playwright install chromium
SEM_ALERTAS=1 python scraper.py
```

## Notas
- O primeiro alerta só pode chegar no segundo dia, porque precisa de um preço anterior para comparar.
- O agendamento usa a hora UTC. Para mudar a hora, edita `cron` em `.github/workflows/diario.yml`.
- Portáteis, PCs montados e bundles com 5070 Ti são ignorados (lista `EXCLUIR` em `scraper.py`).
