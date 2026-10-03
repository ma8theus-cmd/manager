\
# MesLibertines Manager — estado atual

A versão em produção atualmente:

- importa e mantém os membros no SQLite;
- gera username determinístico e email único por membro;
- oferece dois botões independentes: até 5 contas pela rota direta e até 5 pela rota VPN;
- não impõe temporizador entre acionamentos; limites externos do site continuam sendo respeitados;
- usa uma sessão isolada do Chromium por membro;
- executa o fluxo autorizado de +18, Terms e Register e registra os estados no banco;
- permite confirmação de email e ativação de perfil;
- mantém histórico operacional, logs e screenshots de diagnóstico;
- mantém a pesquisa pública de comentários e a fila de Scout separadas dos fluxos de cadastro quando a arquitetura de dois nós é usada.

## Importante

O adapter não tenta resolver CAPTCHA, Cloudflare, rate limit ou bloqueio. Se detectar
algo desse tipo, marca `MANUAL_INTERVENTION`.

## 1. Copiar o projeto para a VPS

Entre na pasta:

```bash
cd ~/meslibertines_manager_v1
```

## 2. Instalar pacotes do Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

## 3. Criar ambiente virtual

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 4. Instalar Python e Chromium do Playwright

```bash
pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium
```

## 5. Criar o .env

```bash
cp .env.example .env
nano .env
```

Edite principalmente:

```env
EMAIL_DOMAIN=batutus.site
UNIVERSAL_ACCOUNT_PASSWORD=COLOQUE_A_SENHA_REAL_AQUI
REGISTRATION_DIRECT_LIMIT=5
REGISTRATION_VPN_DAILY_LIMIT=5
HEADLESS=false
```

Os dois botões do painel são independentes e não há temporizador imposto pelo Manager.
O site pode aplicar seus próprios limites; uma falha de rate limit devolve o membro a
`PENDING` e preserva o registro da tentativa.

Salve no nano com `Ctrl+O`, Enter, depois `Ctrl+X`.

## 6. CSV

Coloque `perfildosmembros.csv` dentro de:

```text
data/perfildosmembros.csv
```

Este ZIP já inclui uma cópia do CSV fornecido nesta conversa.

## 7. Importar

```bash
source .venv/bin/activate
python scripts/import_members.py
```

Você deve ver a quantidade importada/atualizada e inválidos.

## 8. Diagnóstico opcional do formulário

Antes do primeiro preenchimento:

```bash
python scripts/inspect_registration_form.py
```

Ele abre o Chromium e imprime `name`, `id`, `type` etc. dos campos, sem enviar nada.

Se o site tiver mudado o HTML, use essa saída para ajustar:

```text
app/site_adapter/meslibertines.py
```

## 9. Abrir o painel

No terminal da área de trabalho da VPS:

```bash
source .venv/bin/activate
python run.py
```

No Chromium da própria VPS:

```text
http://127.0.0.1:8000
```

## 10. Uso atual

No painel, os dois acionamentos disponíveis são:

- `Preparar lote de 5 contas direto`;
- `Preparar lote de 5 contas com VPN`.

Cada botão reserva até cinco membros `PENDING` da sua própria rota. A operação é
manual e sem temporizador do Manager; o site pode impor limites próprios e devolver
um membro para `PENDING`. Os estados e a tentativa ficam registrados no banco.

O processo abre uma sessão isolada do Chromium por membro. O fluxo autorizado de
+18, Terms e Register é executado pelo agente; bloqueios, CAPTCHA, Cloudflare e
rate limit continuam encaminhados para `MANUAL_INTERVENTION`.

## 11. Após o cadastro

1. Confirme o email no catch-all configurado.
2. No painel, use `✓ Email confirmado`.
3. Quando o perfil estiver elegível, use `Ativar perfil` ou `Ativar todos os perfis pendentes`.
4. Confira `logs/registration.log` e `logs/profile_activation.log` quando houver intervenção.

## 12. Se o Chromium não aparecer

Execute o painel de dentro de um terminal aberto na área de trabalho gráfica da VPS.

Cheque:

```bash
echo $DISPLAY
```

Normalmente haverá algo como `:0`, `:1` ou `:10`.

Se estiver vazio, a sessão que iniciou o servidor não está ligada ao display gráfico.

## 13. Logs e screenshots

Logs:

```text
logs/registration.log
```

Screenshots de falha:

```text
screenshots/
```

## 14. Estados principais

```text
PENDING
→ SELECTED
→ FORM_PREPARING
→ FORM_PREPARED
→ WAITING_EMAIL_CONFIRMATION
→ EMAIL_CONFIRMED
```

Se houver verificação/bloqueio:

```text
MANUAL_INTERVENTION
```

## 15. Pesquisa e Scout

A pesquisa pública mantém os comentários elegíveis no banco de pesquisa e gera lotes
de 50 sem repetição quando há material suficiente. O filtro rejeita conteúdo
promocional, sexual explícito ou sem feedback de serviço/profissionalismo.

Após um comentário publicado, o Scout faz verificações em +6h e +8h, no máximo
duas tentativas. Ele apenas verifica visibilidade e não publica nem altera comentários.

A fila de comentários permite no máximo um envio por profissional a cada 8 horas,
com intervalo global mínimo de 4 minutos entre envios.

Para uma checagem offline segura:

```bash
.venv/bin/python scripts/system_check.py --browser
```

Esse comando usa uma página local de teste; não envia ações ao site.
