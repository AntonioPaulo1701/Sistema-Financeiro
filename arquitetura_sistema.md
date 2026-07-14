# Arquitetura do Sistema Financeiro

> **Documentação viva.** Este arquivo é o mapa técnico do projeto: o que cada arquivo faz,
> quais são as funções principais e como as peças se conectam. Toda alteração de código deve
> atualizar este documento no mesmo turno de desenvolvimento (ver `regras_claude.md`).
>
> **Última atualização:** 11/07/2026

---

## Visão Geral

Sistema financeiro local para clínica odontológica, em Flask + SQLite, rodando no Windows.
Gerencia **contas a pagar** (despesas da clínica), **boletos a receber** (cobranças de
clientes), **faturas de cartão**, **relatórios**, **cobrança automática pelo WhatsApp** e o
**Prontuário Digital do Paciente** (pacientes → orçamentos → checkout, integrado ao
financeiro), com **Barra de Pesquisa Global** de pacientes em todas as telas.

### Fluxo entre as camadas

```
Navegador (templates HTML + static/)
        │  formulários POST / páginas GET
        ▼
app.py (rotas Flask — só coordena; não contém regra de negócio)
        │
        ▼
services.py (toda a regra de negócio)
   │        │           │            │
   ▼        ▼           ▼            ▼
database.py  whatsapp.py  extrato.py  pdf_fatura.py
(SQLite)     (pywhatkit)  (OFX/CSV)   (pdfplumber)

Agendador de Tarefas do Windows ──▶ enviar_diario.py ──▶ services.py + whatsapp.py
(criado/removido por automacao.py)
```

Convenções do projeto:
- Erros de negócio viram `services.ContaInvalida` (ou exceções próprias de cada módulo) e
  chegam à tela como mensagem amigável; nada de stacktrace para o usuário.
- Datas no banco sempre em ISO (`YYYY-MM-DD`); exibição em `dd/mm/aaaa` via `_formatar_data_br`.
- Dinheiro em `REAL` no banco; exibição via filtro Jinja `brl` (= `services.formatar_moeda`).
- Migrações de banco são aditivas: `_garantir_coluna` adiciona colunas sem tocar nos dados.

---

## Arquivos Python

### `app.py` — Rotas Flask (camada web)

**Objetivo:** ponto de entrada do sistema web. Define as rotas, recebe formulários, chama o
`services.py` e renderiza os templates. Não contém regra de negócio: erros de validação vêm
prontos do services e viram mensagens na tela (com código HTTP 400 quando é rejeição).

**Configuração no topo do arquivo:**
- Cria as pastas de upload (`uploads/faturas`, `comprovantes`, `extratos`, `faturas_ofx`).
- Registra o filtro Jinja `brl` (formata moeda) e limita uploads a 10 MB.
- Chama `database.init_db()` na inicialização (roda as migrações).
- Roda com `python app.py` (Flask em modo debug, porta 5000).

**Principais rotas por tela:**

| Tela | Rotas | O que fazem |
|---|---|---|
| Painel | `GET /` | Dashboard com métricas gerais (`services.obter_metricas`) e alertas do Radar de Gargalos (`services.detectar_gargalos_financeiros`) |
| Contas a Pagar | `GET /contas`, `POST /contas/nova`, `POST /contas/<id>/status`, `POST /contas/<id>/excluir` | CRUD de despesas, com parcelamento e alerta de preço |
| Extrato | `POST /contas/extrato`, `POST /contas/extrato/salvar` | Importa OFX/CSV (via `extrato.py`) e lança as saídas como contas |
| Comprovantes | `POST /contas/<id>/comprovante`, `GET /contas/<id>/comprovante` | Upload e visualização do comprovante de pagamento |
| Cadastros | `GET /cadastros` + rotas de `plano-contas`, `categorias`, `fornecedores`, `cartoes`, `procedimentos`, `colaboradores` | CRUD das entidades de apoio (abas na mesma tela; procedimentos = tabela de preços do prontuário) |
| Pacientes | `GET /pacientes`, `POST /pacientes/novo`, `POST /pacientes/<id>/editar`, `POST /pacientes/<id>/excluir` | Lista/cadastro de pacientes; clicar num paciente abre o prontuário |
| Foto do paciente | `POST /pacientes/<id>/foto` (upload), `GET /pacientes/<id>/foto` (exibe) | Upload do avatar: só PNG/JPG, renomeado para `paciente_<id>.<ext>` em `uploads/fotos_pacientes/`; o GET serve com `max_age=0` (trocar a foto atualiza na hora) e 404 sem foto. Foto anterior com outra extensão é apagada (falha na limpeza nunca é fatal — OneDrive pode segurar o arquivo) |
| Prontuário | `GET /pacientes/prontuario/<id>` (`?aba=` cadastro/orcamentos/financeiro) | Prontuário do paciente com 3 sub-abas (dados, orçamentos, financeiro/checkout) |
| Orçamentos | `POST /pacientes/<id>/orcamentos/novo`, `POST /pacientes/<id>/orcamentos/<oid>/aprovar`, `.../excluir`, `.../checkout` | Ciclo do orçamento: criar (itens da tabela de preços) → aprovar → checkout (gera a cobrança) |
| Pesquisa Global | `GET /api/pesquisa_pacientes?q=termo` | Busca dinâmica por nome ou CPF (LIKE); devolve JSON `[{id, nome, cpf}]` para o dropdown da topbar |
| Boletos | `GET /boletos`, `POST /boletos/novo`, `POST /boletos/<id>/status`, `POST /boletos/<id>/excluir` | CRUD dos boletos a receber |
| Régua | `POST /boletos/regua` | Salva dias do lembrete prévio e intervalo de cobrança |
| Meta | `POST /boletos/meta` | Salva a Meta de Arrecadação Mensal (barra de progresso) |
| Recado agendado | `POST /boletos/<id>/agendar-mensagem`, `POST /boletos/<id>/cancelar-mensagem` | Salva/remove o recado customizado do modal de calendário |
| Cobrança manual | `POST /boletos/cobrar` | Dispara agora a cobrança dos vencidos (em segundo plano) |
| Cartão | `GET /cartao`, `POST /cartao/fatura-ofx`, `POST /cartao/<ref>/conciliar`, `GET /cartao/<ref>/pdf` | Faturas por mês, importação OFX e conciliação com PDF |
| Relatórios | `GET /relatorios`, `POST /relatorios/enviar`, `POST /relatorios/auditar` | Relatório por período, envio ao WhatsApp e auditoria com IA |
| Oráculo Financeiro | `GET /oraculo`, `POST /oraculo/simular` | Projeção de fluxo de caixa e simulação de gasto novo (gráfico real × simulado) |
| Destinatários | `POST /destinatarios/novo`, `POST /destinatarios/<id>/excluir` | Números que recebem o relatório diário |
| Automação | `POST /automacao/ativar`, `POST /automacao/desativar` | Cria/remove a tarefa agendada do Windows (via `automacao.py`) |

**Helpers internos:** `_render_contas`, `_render_boletos`, `_render_cartao`, `_render_cadastros`,
`_render_relatorios`, `_render_oraculo`, `_render_pacientes`, `_render_prontuario` (montam o
contexto completo de cada tela), `_parse_periodo` (datas dos relatórios),
`_carregar_transacoes_extrato` / `_carregar_transacoes_fatura_ofx` (guardam o
arquivo importado por token entre o upload e a confirmação), `_status_automacao`.

**Dependências:** importa `automacao`, `database`, `extrato`, `services`, `whatsapp`.
Renderiza todos os templates de `templates/`. Salva arquivos em `uploads/`.

---

### `services.py` — Regra de negócio (o coração do sistema)

**Objetivo:** concentra toda a lógica: validações, consultas SQL, cálculos de métricas,
montagem das mensagens de WhatsApp e integração com a IA. É o único módulo que o `app.py` e
o `enviar_diario.py` chamam para operações de negócio.

**Blocos principais (na ordem do arquivo):**

1. **Configurações e cadastros de apoio**
   - `obter_config` / `salvar_config` — lê/grava pares chave-valor na tabela `configuracoes`.
   - CRUD de `destinatarios`, `contas_bancarias`, `categorias`, `cartoes`, `fornecedores`.
   - `ContaInvalida` — exceção padrão de erro de negócio (vira mensagem na tela).

2. **Contas a pagar**
   - `listar_contas`, `criar_conta` (com parcelamento via `_dividir_em_parcelas`),
     `alternar_status`, `excluir_conta`, `conta_ja_existe` (anti-duplicidade).
   - Alerta de inflação: `_media_historica_preco` + `_registrar_alerta_preco` comparam o valor
     novo com a média histórica do mesmo fornecedor (limite `LIMITE_INFLACAO_PERCENTUAL` = 10%).
   - `salvar_comprovante` / `obter_comprovante` — vínculo do arquivo à conta.

3. **Boletos a receber (clientes da clínica)**
   - `listar_boletos` — lista para a tela, com status de exibição, dias de atraso, selo de
     risco e dados do recado agendado.
   - `calcular_risco_clientes` — **Termômetro de Risco**: média de dias de atraso por cliente
     (boletos pagos com atraso, medidos por `data_pagamento` − `data_vencimento`, + vencidos
     em aberto, com os dias correndo até hoje). Média 0 ou sem histórico = Baixo Risco
     (verde), de 1 a 7 dias = Médio (amarelo), acima de 7 = Alto (vermelho). O texto do
     tooltip (média exata e nº de boletos analisados) vai pronto no dict devolvido; a tela o
     exibe num balão estilizado via `data-tooltip` (ver `main.js`/`style.css`).
   - `criar_boleto` (valida nome, telefone via `_normalizar_telefone_cliente`, valor e data),
     `alternar_status_boleto` (grava/apaga `data_pagamento` — alimenta o Termômetro),
     `excluir_boleto`.

4. **Régua de cobrança automática**
   - `listar_regua_cobranca` — classifica os boletos do dia em `lembrete` (N dias antes,
     configurável), `vence_hoje` e `vencido` (cobrado de N em N dias). `ultima_notificacao`
     trava mensagens duplicadas no mesmo dia.
   - `_mensagem_regua` — texto amigável de cada cenário, personalizado com o primeiro nome.
   - `enviar_regua_cobranca` / `verificar_e_cobrar_boletos_vencidos` (só os vencidos, usado
     pelo botão da tela) / `cobrar_boletos_vencidos_em_segundo_plano` (thread).
   - Configuração: `salvar_config_regua`, `obter_dias_lembrete`, `obter_intervalo_cobranca`.

5. **Meta de Arrecadação Mensal**
   - `obter_meta_mensal` / `salvar_meta_mensal` — meta em R$ na tabela `configuracoes`
     (chave `meta_arrecadacao_mensal`; 0 ou vazio = desligada).
   - `montar_progresso_meta` — percentual do recebido do mês sobre a meta + frase motivacional
     por faixa (`inicio` < 50%, `bom` 50–99%, `atingida` ≥ 100%). Devolve `None` se desligada.

6. **Agendamento de Mensagem Customizada (recados)**
   - `agendar_mensagem_boleto` — valida (texto obrigatório, máx. 1000 caracteres, data hoje ou
     futura) e grava `mensagem_agendada_data/texto` no boleto.
   - `cancelar_mensagem_boleto` — limpa o agendamento.
   - `listar_mensagens_agendadas` — recados com data ≤ hoje (pega dias perdidos com o PC desligado).
   - `enviar_mensagens_agendadas` — dispara o texto exato pelo WhatsApp; sucesso limpa o
     agendamento (disparo único), falha mantém para a próxima varredura.

7. **Métricas e relatórios**
   - `obter_metricas` (dashboard), `obter_metricas_boletos` (cards da tela de boletos),
     `obter_historico_recebimentos` (gráfico de 6 meses), `obter_resumo_periodo`,
     `obter_maiores_ofensores`, `gerar_relatorio_texto` (texto do WhatsApp).

8. **Faturas de cartão**
   - `listar_faturas` (agrupadas por mês de referência), `conciliar_fatura` (compara o total
     do PDF com as contas via `_conciliar_contas_por_valor`).

9. **Auditoria com IA**
   - `auditar_financas_ia` — monta o contexto (`_montar_contexto_auditoria`), chama a API do
     Claude (`claude-opus-4-8`, SDK `anthropic`, import local) e devolve o diagnóstico.
     Exige `ANTHROPIC_API_KEY` no ambiente (decisão de projeto: chave só entra no go-live).

10. **Oráculo Financeiro (Simulador de Cenários)**
    - `projetar_fluxo_caixa(meses=6)` — fluxo previsto por mês (atual + próximos 5), lido do
      banco: **receitas** = boletos (`cobrancas_boletos`, pagos e pendentes) somados por mês de
      vencimento; **despesas** = contas a pagar (`contas`) somadas por mês de vencimento;
      **saldo** = receitas − despesas. Meses sem lançamento entram zerados.
    - `simular_cenario_oraculo(valor_mensal, categoria, duracao_meses)` — valida os três
      parâmetros (valor > 0, categoria obrigatória, duração de 1 a 60 meses) e injeta o gasto
      simulado na projeção: `saldo_simulado = saldo − valor` nos meses dentro da duração
      (a partir do mês atual); depois da duração, o saldo simulado volta ao real. **Nada é
      gravado no banco** — é um ensaio. Devolve a projeção com as duas séries, os parâmetros
      (incluindo custo total = valor × duração) e o veredito: `seguro` (nenhum mês simulado
      negativo) ou `vermelho` (algum mês fecha negativo — com aviso especial se o caixa já
      estava negativo antes do gasto). Constantes: `MESES_PROJECAO_ORACULO`,
      `DURACAO_MAX_SIMULACAO`.

11. **Radar de Gargalos Financeiros**
    - `detectar_gargalos_financeiros()` — alerta de categoria gastando acelerado no mês em
      curso, exibido no topo do Painel Principal. Lógica: usa o último **checkpoint** já
      alcançado no mês (dia 10 ou dia 20 — antes do dia 10 não há ritmo para medir e devolve
      lista vazia). Para cada categoria, soma as contas com **vencimento nos primeiros N dias
      do mês atual** e compara com a **média do mesmo trecho (dias 1..N) dos últimos 6
      meses** — comparação proporcional, nunca 10 dias de agora contra 30 de antes. Dispara
      alerta só quando as duas condições valem juntas: ritmo ≥ 30% acima da média
      (`LIMITE_ACELERACAO_RADAR`) **e** excesso ≥ R$ 100 (`EXCESSO_MINIMO_RADAR`) — evita
      alarme falso em categorias de valor baixo. Categorias com menos de 2 meses de histórico
      no trecho ficam de fora (padrão fraco). Devolve alertas ordenados do estouro maior para
      o menor, com gasto atual, média, excesso em R$ e % acima. Demais constantes:
      `DIAS_CHECKPOINT_RADAR`, `MESES_HISTORICO_RADAR`.

12. **Prontuário Digital do Paciente** (fim do arquivo)
    - Relacionamento central: **pacientes → orcamentos → cobrancas_boletos** — o checkout de
      um orçamento aprovado grava a cobrança com `orcamento_id`, então a receita entra
      automaticamente na Régua de Cobrança, na Meta de Arrecadação, no Termômetro de Risco e
      no Oráculo (que já leem `cobrancas_boletos`).
    - Pacientes: `listar_pacientes` (com contagem de orçamentos), `obter_paciente`,
      `criar_paciente` / `editar_paciente` (validação via `_validar_paciente`: nome
      obrigatório, telefone normalizado pelo mesmo `_normalizar_telefone_cliente` dos
      boletos, CPF opcional com 11 dígitos formatado por `_normalizar_cpf` e único,
      nascimento não pode ser futuro), `excluir_paciente` (bloqueado se houver orçamentos).
    - `pesquisar_pacientes(termo)` — Barra de Pesquisa Global: `LIKE` por nome (com escape de
      `%`/`_`) OU por CPF comparando só os dígitos; termo com menos de 2 caracteres devolve
      vazio; limite de 8 resultados. Cada resultado traz `tem_foto` para o dropdown mostrar
      a miniatura (ou a inicial do nome).
    - Foto do paciente: `salvar_foto_paciente` / `obter_foto_paciente` — coluna `foto` guarda
      só o nome do arquivo; validação de extensão, renomeio único e gravação em disco ficam
      na rota (`app.py`).
    - Tabela de preços e equipe: CRUD de `procedimentos_tabela` (`listar/adicionar/
      remover_procedimento`; valor aceita vírgula) e de `colaboradores`
      (`listar/adicionar/remover_colaborador`) — gerenciados nas sub-abas de Cadastros.
    - Orçamentos: `criar_orcamento` (monta `descricao_itens` com nome + valor de cada
      procedimento escolhido e soma o `valor_total`), `listar_orcamentos_paciente` (com
      colaboradores e situação da cobrança vinculada), `aprovar_orcamento` (só pendente),
      `excluir_orcamento` (bloqueado se faturado). Status: `pendente → aprovado → faturado`
      (`STATUS_ORCAMENTO_LABELS`).
    - `efetuar_checkout(orcamento_id, forma, colaborador_id, vencimento)` — só para
      aprovados; `avista` grava a cobrança já **paga hoje** (entra no Recebido do mês),
      `boleto` grava cobrança **pendente** com o vencimento escolhido (não pode ser passado)
      que cai na régua automática. O orçamento vira `faturado` e guarda `checkout_forma`,
      `checkout_colaborador_id` e `checkout_em`. Formas em `FORMAS_CHECKOUT`.
    - `resumo_financeiro_paciente` — totais por status para os cards da sub-aba Financeiro.

**Dependências:** `database.get_connection` (todas as consultas SQL), `whatsapp` (import local
dentro das funções de envio, evitando dependência circular), `anthropic` (import local),
módulos padrão (`datetime`, `calendar`, `re`, `os`, `time`, `threading` via funções).

---

### `database.py` — Conexão e migrações do SQLite

**Objetivo:** cria/abre o banco `financas.db`, garante o esquema e roda migrações aditivas.

**Principais funções:**
- `get_connection()` — conexão SQLite com `row_factory` (linhas viram dict-like).
- `_garantir_coluna(conexao, tabela, coluna, tipo)` — `ALTER TABLE` só se a coluna não existir.
- `init_db()` — cria todas as tabelas (`IF NOT EXISTS`) e aplica as colunas novas.

**Tabelas do banco:**

| Tabela | Guarda | Colunas adicionadas por migração |
|---|---|---|
| `contas` | Despesas a pagar da clínica | `forma_pagamento`, `fatura_referencia`, `conciliacao_pdf`, `fornecedor_id`, `comprovante`, `grupo_parcelamento`, `conta_bancaria_id`, `cartao_id` |
| `faturas` | Faturas de cartão por mês de referência | `total_detectado` |
| `configuracoes` | Pares chave-valor (hora do envio, régua, `meta_arrecadacao_mensal`) | — |
| `categorias` | Categorias de despesa | — |
| `contas_bancarias` | Plano de contas (de onde sai o dinheiro) | — |
| `cartoes` | Cartões de crédito cadastrados | — |
| `fornecedores` | Fornecedores (para alerta de preço) | — |
| `alertas_preco` | Histórico de alertas de inflação | — |
| `destinatarios` | Números que recebem o relatório diário | — |
| `cobrancas_boletos` | Boletos a receber dos clientes | `ultima_notificacao` (régua), `data_pagamento` (Termômetro de Risco), `mensagem_agendada_data` + `mensagem_agendada_texto` (recados customizados), `orcamento_id` (elo com o checkout do prontuário) |
| `pacientes` | Pacientes da clínica (nome, telefone normalizado, CPF, nascimento) | `foto` (nome do arquivo em `uploads/fotos_pacientes/`) |
| `procedimentos_tabela` | Tabela de preços da clínica (`nome_procedimento`, `valor_base`) | — |
| `colaboradores` | Equipe da clínica — responsáveis por orçamentos e checkouts | — |
| `orcamentos` | Orçamentos do prontuário (`paciente_id`, `colaborador_id`, `descricao_itens`, `valor_total`, `status` pendente/aprovado/faturado, `checkout_forma` + `checkout_colaborador_id` + `checkout_em`) | — |

**Relacionamento do Prontuário:** `pacientes` 1—N `orcamentos` (via `orcamentos.paciente_id`)
e `orcamentos` 1—1 `cobrancas_boletos` (via `cobrancas_boletos.orcamento_id`, criada no
checkout). Assim toda entrada do prontuário desagua no mesmo fluxo financeiro dos boletos:
régua de cobrança, meta mensal, termômetro de risco e projeções do Oráculo.

**Dependências:** só `sqlite3` e `logging`. É importado por `services.py`, `app.py` e
`enviar_diario.py` (que também chama `init_db()` antes de rodar).

---

### `whatsapp.py` — Motor de envio pelo WhatsApp

**Objetivo:** única porta de saída de mensagens. Usa `pywhatkit`, que abre o WhatsApp Web no
navegador padrão (precisa estar logado via QR code) e envia sozinho.

**Principais funções:**
- `validar_numero` — exige formato internacional `+5511999999999` (regex `PADRAO_NUMERO`).
- `enviar_mensagem(numero, mensagem)` — envio unitário via `pywhatkit.sendwhatmsg_instantly`.
- `enviar_para_varios` — lote sequencial com pausa de `INTERVALO_ENTRE_ENVIOS` (120 s) entre
  números; devolve `(numero, sucesso, erro)` por destinatário.
- `enviar_para_varios_em_segundo_plano` — mesma coisa numa thread daemon (a tela não trava).
- `EnvioWhatsAppInvalido` — exceção padrão de falha de envio.

**Dependências:** `pywhatkit` (import local dentro de `enviar_mensagem`). Não toca no banco.
Quem chama: `services.py` (régua, recados), `app.py` (relatórios) e `enviar_diario.py`.

---

### `automacao.py` — Agendador de tarefas do Windows

**Objetivo:** liga/desliga o envio automático diário criando a tarefa
`config.NOME_TAREFA_AGENDADA` no Agendador de Tarefas nativo (`schtasks`), que executa o
`enviar_diario.py` no horário escolhido — mesmo com o sistema web fechado.

**Principais funções:** `tarefa_ativa()`, `ativar_envio_diario(hora)` (valida `HH:MM`,
`schtasks /Create ... /SC DAILY`), `desativar_envio_diario()`, exceção `AutomacaoErro`.

**Dependências:** `subprocess` (schtasks). Não toca no banco nem no WhatsApp diretamente.
Chamado apenas pelas rotas `/automacao/*` do `app.py`.

---

### `enviar_diario.py` — Script do envio automático (roda fora do Flask)

**Objetivo:** executado pelo Agendador de Tarefas do Windows. Roda as três etapas do dia,
**independentes entre si** (falha em uma não impede as outras), e registra tudo em
`logs/envio_automatico.log`.

**Etapas (funções):**
1. `_enviar_relatorio_do_dia` — gera o relatório de hoje (`services.gerar_relatorio_texto`) e
   envia para todos os `destinatarios`.
2. `_enviar_regua_cobranca` — roda a régua completa (`services.enviar_regua_cobranca`).
3. `_enviar_recados_agendados` — dispara os recados customizados do dia
   (`services.enviar_mensagens_agendadas`); recado que falha fica para a próxima varredura.

`main()` chama `database.init_db()` antes de tudo e devolve código de saída 0/1.

**Dependências:** `database`, `services`, `whatsapp` (imports dentro de `main` para o log já
estar configurado). Grava em `logs/`.

---

### `extrato.py` — Leitor de extratos bancários (OFX/CSV)

**Objetivo:** transforma o arquivo do banco em transações de **saída** prontas para virar
conta (data ISO, descrição, valor positivo), sem digitação manual. Sem dependência externa:
OFX via regex, CSV via módulo padrão, com heurísticas para formatos brasileiros.

**Principais funções:** `ler_extrato(caminho)` (ponto de entrada; despacha por extensão),
`ler_fatura_cartao(caminho)` (variante para OFX de cartão), `_ler_ofx`, `_ler_csv`,
`_converter_valor` (aceita `1.234,56`, `-R$ 50,00`, `(100,00)`), `_converter_data`
(5 formatos), `_ler_texto` (UTF-8/Latin-1), exceção `LeituraExtratoErro`.

**Dependências:** só biblioteca padrão. Chamado pelas rotas de importação do `app.py`.

---

### `pdf_fatura.py` — Leitor do PDF da fatura de cartão

**Objetivo:** extrai os valores monetários (padrão brasileiro) do texto do PDF para a
conciliação automática da fatura com as contas cadastradas.

**Principais funções:** `extrair_dados_fatura(caminho_pdf)` — devolve `(valores,
total_detectado)`; o total é procurado nas linhas com palavras de `PALAVRAS_TOTAL` ("total da
fatura" etc.). PDF digitalizado (imagem) devolve lista vazia. Exceção `LeituraFaturaErro`.

**Dependências:** `pdfplumber`. Chamado por `services.conciliar_fatura`.

---

## Templates (pasta `templates/`)

Todos estendem `base.html` e são renderizados pelo `app.py`. O contexto de cada tela é
montado pelos helpers `_render_*`.

| Template | Tela | Pontos importantes |
|---|---|---|
| `base.html` | Layout comum | Sidebar de navegação (inclui Pacientes), topo com **Barra de Pesquisa Global de Pacientes** (`data-pesquisa-global`, dropdown de resultados) + data + botões 🧘 (Modo Focus) e sol/lua (tema), botão flutuante `.focus-sair`, blocos `content` e `extra_scripts`; carrega Google Fonts (Inter/JetBrains Mono), `style.css`, `main.js` e o script inline anti-flash do tema e do Modo Focus salvos |
| `dashboard.html` | Painel Principal (`/`) | Cards de alerta do **Radar de Gargalos** no topo (`radar-alerta`, com link para a Gestão de Contas), cards de métricas gerais e visão do mês |
| `contas.html` | Contas a Pagar (`/contas`) | Formulário com parcelamento, importação de extrato OFX/CSV, comprovantes, alerta de preço |
| `boletos.html` | Cobrança de Boletos (`/boletos`) | Barra da **Meta de Arrecadação** (topo), cards + 2 gráficos Chart.js, formulário de boleto, tabela com **selo de risco** (`risco-selo`) e botão de calendário que abre o **modal de recado** (`<dialog id="modal-recado">`, JS `abrirModalRecado`), painéis de configuração da Régua e da Meta |
| `cartao.html` | Fatura de Cartão (`/cartao`) | Faturas por mês, upload de PDF/OFX, conciliação |
| `cadastros.html` | Cadastros (`/cadastros`) | Abas: Plano de Contas, Categorias, Fornecedores, Cartões, Procedimentos (tabela de preços do prontuário) e Colaboradores |
| `pacientes.html` | Pacientes (`/pacientes`) | Formulário de novo paciente (telefone normalizado, CPF/nascimento opcionais) e tabela com link para o prontuário de cada um |
| `prontuario.html` | Prontuário (`/pacientes/prontuario/<id>`) | 3 sub-abas (padrão `aba-botao`/`data-aba-conteudo`, `?aba=`): **Cadastro** (avatar circular `paciente-avatar` — foto ou inicial do nome — com botão "📷 Alterar Foto" que dispara o input de arquivo escondido e envia sozinho ao escolher; editar/excluir), **Orçamentos** (grade de procedimentos com total em tempo real + tabela com Aprovar/Excluir) e **Financeiro/Checkout** (cards por status, botão "💰 Efetuar Checkout" que abre o `<dialog id="modal-checkout">` com forma de pagamento à vista/boleto — campo de vencimento só aparece no boleto — e colaborador responsável; histórico de checkouts com a situação da cobrança gerada) |
| `relatorios.html` | Central de Relatórios (`/relatorios`) | Período, texto do relatório, envio WhatsApp, destinatários, automação diária, auditoria com IA |
| `oraculo.html` | Oráculo Financeiro (`/oraculo`) | Formulário da simulação (valor mensal, categoria, duração), painel de veredito (`oraculo-veredito` seguro/vermelho), gráfico de linha Chart.js comparando "Fluxo de Caixa Real Atual" (teal, sólida) × "Com o Cenário Simulado" (rosa, tracejada com losangos — distinguível sem cor), linha do zero destacada e tabela expansível (`<details>`) com os números |

---

## Estáticos (pasta `static/`)

### `static/css/style.css`
Todo o visual do sistema. Variáveis de cor em `:root` (`--brand-teal`, `--brand-pink`,
`--color-success/warning/danger` + fundos `-bg`). Componentes principais: layout com sidebar,
`stat-tile` (cards), `panel`, tabelas, formulários (`form-field`, `form-hint`), botões
(`btn`, `btn-icon`), `status-badge` (pago/pendente/vencido), `risco-selo`
(baixo/medio/alto — Termômetro de Risco), `tooltip-balao` (balão dos elementos com
`data-tooltip`, posicionado pelo `main.js`), `meta-*` (barra da Meta de Arrecadação),
`modal-recado` (dialog do recado customizado), `regua-*` (painel da régua),
`oraculo-veredito` + `oraculo-tabela` (Oráculo Financeiro), `radar-gargalos` +
`radar-alerta` (cards do Radar de Gargalos no painel), `topbar-right` + `tema-toggle`
(botão sol/lua do alternador de temas), `modo-focus-ativo` + `focus-sair` (Modo Focus —
ver seção abaixo), `pesquisa-global` + `pesquisa-resultados`/`pesquisa-item` + `pesquisa-foto` (Barra de
Pesquisa Global da topbar, com miniatura da foto ou inicial), `paciente-avatar` +
`paciente-foto-bloco` (foto circular do prontuário), `procedimento-opcao` (grade de procedimentos do orçamento,
com realce via `:has(input:checked)`), `orcamento-itens`, `checkout-formas` +
`checkout-forma-opcao` (modal de checkout) e as variantes `status-badge.aprovado`/
`.faturado` (status do orçamento).

### Modo Focus (todas as telas)

Modo de tela limpa: esconde sidebar e topbar e deixa o conteúdo da tela — filtros,
cards, tabelas e gráficos — com 100% da largura. Disponível em todas as abas.

- **Classe global:** `.modo-focus-ativo` no `<body>`. Nenhum elemento é removido do DOM —
  o CSS apenas reage à classe, então sair do modo restaura a tela exatamente como estava.
- **O que o CSS faz** (seção "Modo Focus" no `style.css`): `.sidebar`, `.topbar` e
  `.content` ganham `transition: all 0.3s ease`. Com a classe ativa, a sidebar desliza
  para fora (`margin-left: calc(-1 * var(--sidebar-width))` + `opacity: 0` +
  `visibility: hidden` — o margin negativo libera o espaço no flex, expandindo o conteúdo)
  e a topbar recolhe (`max-height: 0` + paddings zerados + `opacity: 0`; a regra base fixa
  `max-height: 200px` como valor de partida, porque `max-height: none → 0` não anima).
  Como o `.content` é `flex: 1`, o conteúdo passa a ocupar a largura toda sem regra extra.
  O botão flutuante `.focus-sair` (canto superior direito, `position: fixed`,
  semitransparente até o hover) só aparece com a classe ativa — é a saída visível, já que
  a topbar (onde mora o botão de entrar) fica escondida no modo.
- **Interação** (`initModoFocus` no `main.js`): dois botões com `data-focus-toggle`
  alternam a classe — o 🧘 da topbar (ao lado do sol/lua, em todas as telas, estilo
  `tema-toggle`) e o flutuante "🧘 Sair do Modo Focus". Atalhos de teclado: **F**
  liga/desliga; **Esc** só desliga. Os atalhos são ignorados quando o usuário está
  digitando num campo (input/textarea/select), quando há modificador (Ctrl/Alt/Meta —
  preserva o Ctrl+F do navegador) e, no caso do Esc, quando há um calendário `dp-*` aberto
  (o Esc fecha só o calendário).
- **Persistência:** a escolha fica no `localStorage` (chave `modo_focus`, valores
  `ativo`/`inativo`) e vale para o sistema inteiro — sobrevive a recarregamentos e à troca
  de tela. O script inline do `base.html` (o mesmo do anti-flash do tema) reaplica a
  classe antes da primeira pintura, então a tela já nasce sem os menus, sem piscar.

### Sistema de temas (Light padrão × Cyberpunk Dark Mode)

**Onde os tokens vivem:** todas as cores do sistema são variáveis CSS declaradas em `:root`
no topo do `style.css` (tema claro, o padrão). No **fim do arquivo**, o bloco
`body.dark-mode` sobrescreve essas mesmas variáveis com a paleta "Painel de Comando
Holográfico" — nenhum componente referencia cor de tema diretamente; tudo passa pelos tokens.

**Paleta do dark mode (True Black & Neon):**

| Token | Valor escuro | Papel |
|---|---|---|
| `--color-bg` | `#0a0a0c` | Fundo true black (com glows radiais ciano/esmeralda sutis) |
| `--color-surface` | `#101017` | Superfície dos painéis (usada translúcida no vidro) |
| `--color-success` | `#00e69b` | **Verde Esmeralda Elétrico** — pagos/recebidos |
| `--brand-teal` / `--color-primary` | `#22d3ee` | **Ciano Holográfico** — previstos, navegação, ação primária |
| `--color-danger` | `#ff3b5c` | **Vermelho Neon** — vencidos e alertas |
| `--color-warning` | `#fbbf24` | Âmbar — pendências |
| `--chart-grid` | `rgba(255,255,255,.07)` | Grade dos gráficos (no claro: `#efe6f7`) |

Fundos de status (`--color-*-bg`) viram `rgba` translúcidos. Efeitos exclusivos do escuro:
**glassmorphism** nos `stat-tile`/`panel`/`fatura-card` (fundo `rgba` + `backdrop-filter:
blur(14px)`) com **borda gradiente de 1px** na cor neon do card (técnica
`padding-box`/`border-box` + `color-mix` sobre `--tone-color`). **Atenção ao empilhamento:**
o `backdrop-filter` cria um contexto de empilhamento por painel, então popovers absolutos
dentro de um painel seriam cobertos pelo painel seguinte; por isso
`.panel:has(.dp-wrapper.aberto)` ganha `position: relative; z-index: 80` (eleva o painel
com calendário aberto) e a `.topbar` é `position: relative; z-index: 55` (o dropdown da
pesquisa global flutua sobre o conteúdo). Qualquer popover novo dentro de painel precisa
do mesmo tratamento; tabelas sem divisórias
pesadas (linhas `rgba(255,255,255,.04)`) com hover ciano; tipografia **Inter** no corpo e
**JetBrains Mono** nos números (`stat-value`, `topbar-date`, `meta-percentual` etc.), fontes
carregadas do Google Fonts no `base.html`. O tema claro permanece exatamente como era.

**Alternância:** botão sol/lua (`data-tema-toggle`) no canto superior direito da topbar
(`base.html`). O `initTemaToggle` do `main.js` adiciona/remove `dark-mode` no `<body>`,
salva a escolha em `localStorage` (chave `tema`, valores `dark`/`light`) e dispara o evento
`temaAlterado`. Um script inline logo após `<body>` no `base.html` reaplica o tema salvo
**antes da primeira pintura** (evita flash claro ao abrir).

**Gráficos:** os scripts de `boletos.html` e `oraculo.html` não têm mais cores fixas — a
função `coresTema()` lê as variáveis do tema ativo via `getComputedStyle`, a montagem fica
em `montarGrafico(s)()` e um listener de `temaAlterado` destrói e reconstrói os gráficos com
a paleta nova na hora da troca.

### `static/js/main.js`
Interações puramente visuais (a lógica real é sempre do backend): data do topo, copiar
relatório, atalhos de data, calendário próprio (`initDatePickers`/`criarDatePicker` — o
seletor de ano mostra 6 anos para trás por padrão, mas campos cujo `name` contém
"nascimento" ganham 100 anos para trás, senão não dá para escolher o ano de nascimento
do paciente), prévia da mensagem de WhatsApp, upload de comprovante, abas de
cadastros, `initTooltips` (balão único `position: fixed` para qualquer elemento com
`data-tooltip` — usado pelo selo de risco; fixo para não ser cortado pelo `overflow-x` do
`.table-wrapper`), `initTemaToggle` (alternador claro/escuro — ver "Sistema de temas"
abaixo), `initModoFocus` (Modo Focus — ver seção própria acima) e `initPesquisaGlobal`
(Barra de Pesquisa Global: escuta o campo da topbar com pausa de 250 ms entre teclas,
aborta a busca anterior em voo via `AbortController`, consulta `/api/pesquisa_pacientes`
e monta o dropdown — cada item com a miniatura da foto do paciente ou um círculo com a
inicial quando `tem_foto` é falso; clicar num resultado navega para
`/pacientes/prontuario/<id>`; Esc ou clique fora fecham; termos com menos de 2 caracteres
não disparam busca). O JS
específico dos boletos (gráficos Chart.js e modal de recado) vive no bloco
`extra_scripts` do próprio `boletos.html`; o do prontuário (modal de checkout, campo de
vencimento condicional e total do orçamento em tempo real) no de `prontuario.html`.

---

## Dados e artefatos locais

| Item | Conteúdo |
|---|---|
| `financas.db` | Banco SQLite (esquema na seção `database.py`) |
| `uploads/comprovantes/` | Comprovantes de pagamento das contas |
| `uploads/extratos/` | Extratos OFX/CSV importados (guardados por token durante a importação) |
| `uploads/faturas/` | PDFs de fatura de cartão |
| `uploads/faturas_ofx/` | OFX de fatura de cartão |
| `uploads/fotos_pacientes/` | Fotos dos pacientes (`paciente_<id>.png/.jpg`), servidas por `GET /pacientes/<id>/foto` |
| `logs/envio_automatico.log` | Registro de cada execução do `enviar_diario.py` |
| `requirements.txt` | Flask, pywhatkit, pdfplumber, anthropic |
| `regras_claude.md` | Diretrizes de desenvolvimento (inclui a regra da documentação viva) |
| `projeto_atual.md` | Histórico/estado do projeto |
| `PyWhatKit_DB.txt` | Arquivo gerado automaticamente pelo pywhatkit (não editar) |
| `.gitignore` | Impede o versionamento de dados locais: banco (`*.db`), `venv/`, `__pycache__/`, `.env`, `uploads/`, `logs/` e `PyWhatKit_DB.txt` |
| `README.md` | Apresentação do projeto no GitHub (repositório privado de backup; commits locais são enviados para lá) |

---

## Onde procurar quando algo der errado

- **Erro numa tela:** ache a rota em `app.py` (tabela acima) → a regra chamada está em
  `services.py`; mensagens `ContaInvalida` indicam rejeição de validação (HTTP 400).
- **Mensagem de WhatsApp não saiu:** `logs/envio_automatico.log` (envio automático) ou o
  terminal do Flask (envio manual). Verifique WhatsApp Web logado; exceção padrão é
  `EnvioWhatsAppInvalido` em `whatsapp.py`.
- **Envio automático não rodou:** confira a tarefa `NOME_TAREFA_AGENDADA` (ver `config.py`) no Agendador
  de Tarefas do Windows (`automacao.py`) e o log acima.
- **Dado estranho no banco:** esquema e migrações em `database.py`; o banco é `financas.db`.
- **Visual quebrado:** classe CSS em `static/css/style.css`; comportamento em `main.js` ou no
  `extra_scripts` do template da tela.
- **Importação de extrato/fatura falhou:** `extrato.py` (OFX/CSV) ou `pdf_fatura.py` (PDF);
  ambos têm exceções próprias com mensagens claras.
- **Auditoria com IA falhou:** exige `ANTHROPIC_API_KEY` no ambiente (só configurada no
  go-live, por decisão de projeto); erros da API viram `ContaInvalida` com orientação.
- **Prontuário/checkout com problema:** rotas em `app.py` (tabela acima), regras no bloco
  "Prontuário Digital do Paciente" no fim do `services.py`. A cobrança gerada pelo checkout
  aparece na aba Cobrança de Boletos com o `orcamento_id` preenchido no banco.
- **Pesquisa global sem resultados:** rota `GET /api/pesquisa_pacientes` →
  `services.pesquisar_pacientes` (LIKE por nome/CPF, mínimo 2 caracteres); o dropdown é
  montado por `initPesquisaGlobal` no `main.js`.
