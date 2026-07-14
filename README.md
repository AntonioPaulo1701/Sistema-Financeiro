# 🦷 Sistema Financeiro para Clínica Odontológica

> Sistema de gestão financeira e prontuário digital para clínica odontopediátrica,
> com cobrança automática pelo WhatsApp e análises com Inteligência Artificial.

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-Web-000000?logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Banco%20local-003B57?logo=sqlite&logoColor=white)
![WhatsApp](https://img.shields.io/badge/WhatsApp-Cobran%C3%A7a%20autom%C3%A1tica-25D366?logo=whatsapp&logoColor=white)
![Claude](https://img.shields.io/badge/Claude%20AI-An%C3%A1lises%20inteligentes-D97757)
![Status](https://img.shields.io/badge/Status-Em%20desenvolvimento-yellow)

---

## 📋 O que o sistema faz

Sistema completo que roda **localmente no Windows**, feito sob medida para a rotina da
clínica — sem mensalidade, sem depender de internet para o dia a dia e com os dados
sempre em casa.

### 💸 Financeiro

| Módulo | Descrição |
|---|---|
| **Painel** | Dashboard com métricas gerais e o *Radar de Gargalos* (alertas automáticos de problemas financeiros) |
| **Contas a Pagar** | Despesas da clínica com parcelamento, alerta de preço, comprovantes anexados e importação de extrato bancário (OFX/CSV) |
| **Boletos a Receber** | Cobranças de clientes com régua de cobrança, meta mensal de arrecadação e recados agendados |
| **Cartão de Crédito** | Faturas por mês, importação OFX e conciliação automática com o PDF da fatura |
| **Relatórios** | Relatório por período, envio pelo WhatsApp e auditoria com IA |
| **Oráculo Financeiro** | Projeção de fluxo de caixa e simulador "e se eu gastar X?" com gráfico comparativo |

### 🧒 Prontuário Digital do Paciente

- Cadastro de pacientes com foto e busca global (nome ou CPF) em todas as telas
- Orçamentos montados a partir da tabela de preços de procedimentos
- Ciclo completo: **orçamento → aprovação → checkout**, integrado direto ao financeiro

### 🤖 Automação

- **Cobrança automática pelo WhatsApp**: lembrete antes do vencimento e cobrança dos
  boletos vencidos, no intervalo configurado
- **Relatório diário** enviado pelo WhatsApp aos destinatários cadastrados, agendado
  pelo Agendador de Tarefas do Windows
- **Análises com IA (Claude)**: auditoria de lançamentos e insights nos relatórios

---

## 🏗️ Arquitetura

```
Navegador (templates HTML + static/)
        │  formulários POST / páginas GET
        ▼
app.py (rotas Flask — só coordena)
        │
        ▼
services.py (toda a regra de negócio)
   │        │           │            │
   ▼        ▼           ▼            ▼
database.py  whatsapp.py  extrato.py  pdf_fatura.py
(SQLite)     (pywhatkit)  (OFX/CSV)   (pdfplumber)

Agendador do Windows ──▶ enviar_diario.py ──▶ services.py + whatsapp.py
```

A documentação técnica completa — cada arquivo, cada função, cada rota — está em
[`arquitetura_sistema.md`](arquitetura_sistema.md) (documentação viva, atualizada a
cada mudança de código).

---

## 🚀 Como rodar

```bash
# 1. Criar e ativar o ambiente virtual
python -m venv venv
venv\Scripts\activate

# 2. Instalar as dependências
pip install -r requirements.txt

# 3. Iniciar o sistema
python app.py
```

Depois é só abrir **http://localhost:5000** no navegador. O banco de dados
(`financas.db`) é criado automaticamente na primeira execução.

> **WhatsApp:** os envios usam o WhatsApp Web — é preciso estar logado no navegador
> padrão. **IA:** os recursos de análise exigem a variável de ambiente
> `ANTHROPIC_API_KEY` (planejados para o go-live).

---

## ⚙️ Configuração

O nome da clínica não fica escrito no código — ele vem do ambiente (ver
[`config.py`](config.py)), para que o sistema sirva a qualquer consultório.

| Variável | Padrão | Para que serve |
|---|---|---|
| `NOME_CLINICA` | `Clínica Odontológica` | Nome exibido nas telas e nas mensagens de WhatsApp |
| `NOME_TAREFA_AGENDADA` | `SistemaFinanceiro_RelatorioDiario` | Nome da tarefa no Agendador do Windows |
| `ANTHROPIC_API_KEY` | — | Habilita a auditoria financeira com IA |

No Windows, para que valham também na tarefa agendada:

```bash
setx NOME_CLINICA "Nome da Sua Clínica"
setx NOME_TAREFA_AGENDADA "NomeDaSuaClinica_RelatorioDiario"
```

> ⚠️ Se você já tem a tarefa diária criada com outro nome, mantenha o valor antigo em
> `NOME_TAREFA_AGENDADA` — ou reagende o horário pela Central de Relatórios e apague a
> tarefa antiga no Agendador de Tarefas.

---

## 🔒 Privacidade

Este repositório contém **apenas o código-fonte**. O banco de dados, os comprovantes,
as fotos de pacientes e os logs ficam somente na máquina da clínica e nunca são
versionados (ver [`.gitignore`](.gitignore)).

---

## 🛠️ Tecnologias

- **[Flask](https://flask.palletsprojects.com/)** — servidor web e rotas
- **[SQLite](https://www.sqlite.org/)** — banco de dados local, sem instalação
- **[pywhatkit](https://github.com/Ankit404butfound/PyWhatKit)** — envio de mensagens pelo WhatsApp Web
- **[pdfplumber](https://github.com/jsvine/pdfplumber)** — leitura dos PDFs de fatura de cartão
- **[Claude (Anthropic)](https://www.anthropic.com/)** — auditoria e análises inteligentes

---

<p align="center">
  Feito com 💙 para a odontopediatria.
</p>
