import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "financas.db"

# Categorias padrão do plano de contas (criadas na primeira execução; o usuário
# pode adicionar/remover as suas). Os códigos antigos ('fixas' etc.) eram
# gravados direto nas contas e são migrados para o nome legível.
CATEGORIAS_PADRAO = {
    "fixas": "Contas Fixas",
    "fornecedores": "Fornecedores",
    "utilidades": "Utilidades",
    "impostos": "Impostos",
    "outros": "Outros",
}


def get_connection():
    conexao = sqlite3.connect(DB_PATH)
    conexao.row_factory = sqlite3.Row
    return conexao


def _garantir_coluna(conexao, tabela, coluna, definicao):
    """Adiciona uma coluna à tabela caso ela ainda não exista (migração simples)."""
    colunas_existentes = {linha["name"] for linha in conexao.execute(f"PRAGMA table_info({tabela})")}
    if coluna not in colunas_existentes:
        conexao.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")
        logger.info("Coluna '%s' adicionada à tabela '%s'.", coluna, tabela)


def init_db():
    """Cria/atualiza as tabelas do sistema caso ainda não existam."""
    try:
        with get_connection() as conexao:
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS contas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL,
                    valor REAL NOT NULL,
                    vencimento TEXT NOT NULL,
                    categoria TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    pago_em TEXT,
                    forma_pagamento TEXT NOT NULL DEFAULT 'outro',
                    fatura_referencia TEXT,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Bancos criados antes do módulo de cartão de crédito precisam
            # ganhar as colunas novas sem perder os dados já cadastrados.
            _garantir_coluna(conexao, "contas", "forma_pagamento", "TEXT NOT NULL DEFAULT 'outro'")
            _garantir_coluna(conexao, "contas", "fatura_referencia", "TEXT")
            # Resultado da conciliação automática com o PDF da fatura:
            # NULL (ainda não conciliada), 'encontrada' ou 'nao_encontrada'.
            _garantir_coluna(conexao, "contas", "conciliacao_pdf", "TEXT")
            # Fornecedor vinculado (opcional), arquivo do comprovante de pagamento
            # e identificador que agrupa as parcelas de uma mesma compra.
            _garantir_coluna(conexao, "contas", "fornecedor_id", "INTEGER")
            _garantir_coluna(conexao, "contas", "comprovante", "TEXT")
            _garantir_coluna(conexao, "contas", "grupo_parcelamento", "TEXT")

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS faturas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referencia TEXT NOT NULL UNIQUE,
                    valor_informado REAL,
                    arquivo_pdf TEXT,
                    status TEXT NOT NULL DEFAULT 'aberta',
                    conciliada_em TEXT
                )
                """
            )
            # Total lido automaticamente do PDF (diferente do informado à mão).
            _garantir_coluna(conexao, "faturas", "total_detectado", "REAL")

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS configuracoes (
                    chave TEXT PRIMARY KEY,
                    valor TEXT
                )
                """
            )

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS categorias (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL UNIQUE,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            if conexao.execute("SELECT COUNT(*) AS qtd FROM categorias").fetchone()["qtd"] == 0:
                conexao.executemany(
                    "INSERT INTO categorias (nome) VALUES (?)",
                    [(nome,) for nome in CATEGORIAS_PADRAO.values()],
                )
                logger.info("Plano de contas criado com as %d categorias padrão.", len(CATEGORIAS_PADRAO))
            # Contas antigas guardavam o código da categoria; migra para o nome
            # legível (idempotente: nada muda se já estiver migrado).
            for codigo, nome in CATEGORIAS_PADRAO.items():
                conexao.execute("UPDATE contas SET categoria = ? WHERE categoria = ?", (nome, codigo))

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS contas_bancarias (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL UNIQUE,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # De qual conta do plano de contas saiu o dinheiro do pagamento.
            _garantir_coluna(conexao, "contas", "conta_bancaria_id", "INTEGER")

            # Cartões de crédito da clínica (ex: Nubank, Bradesco Visa) — usados
            # na importação de faturas OFX para marcar de qual cartão é o gasto.
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS cartoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL UNIQUE,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            _garantir_coluna(conexao, "contas", "cartao_id", "INTEGER")

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS fornecedores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL UNIQUE,
                    telefone TEXT,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

            # Monitoramento de preços: alerta gravado quando uma conta nova fica
            # mais de 10% acima da média histórica do mesmo nome/fornecedor.
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS alertas_preco (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conta_id INTEGER NOT NULL UNIQUE,
                    valor_novo REAL NOT NULL,
                    media_historica REAL NOT NULL,
                    percentual REAL NOT NULL,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS destinatarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    apelido TEXT,
                    numero TEXT NOT NULL UNIQUE,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Gestão e Cobrança de Boletos: boletos emitidos para clientes,
            # cobrados via WhatsApp quando vencem.
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS cobrancas_boletos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome_cliente TEXT NOT NULL,
                    telefone TEXT NOT NULL,
                    valor REAL NOT NULL,
                    data_vencimento TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Régua de cobrança: data (YYYY-MM-DD) da última mensagem enviada ao
            # cliente deste boleto — evita avisos duplicados no mesmo dia.
            _garantir_coluna(conexao, "cobrancas_boletos", "ultima_notificacao", "TEXT")
            # Termômetro de Risco: data (YYYY-MM-DD) em que o boleto foi marcado
            # como pago — permite medir o atraso real de cada pagamento.
            _garantir_coluna(conexao, "cobrancas_boletos", "data_pagamento", "TEXT")
            # Agendamento de Mensagem Customizada: recado avulso que o envio
            # diário dispara pelo WhatsApp na data marcada (e então limpa).
            _garantir_coluna(conexao, "cobrancas_boletos", "mensagem_agendada_data", "TEXT")
            _garantir_coluna(conexao, "cobrancas_boletos", "mensagem_agendada_texto", "TEXT")

            # ---- Prontuário Digital do Paciente ----
            # Relacionamento: pacientes -> orcamentos -> cobrancas_boletos
            # (o checkout de um orçamento aprovado gera a cobrança financeira).
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS pacientes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL,
                    telefone TEXT NOT NULL,
                    cpf TEXT,
                    data_nascimento TEXT,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Tabela de preços da clínica (procedimentos oferecidos).
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS procedimentos_tabela (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome_procedimento TEXT NOT NULL UNIQUE,
                    valor_base REAL NOT NULL,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Colaboradores da clínica: responsáveis por orçamentos e checkouts.
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS colaboradores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL UNIQUE,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Orçamentos do prontuário. Status: pendente -> aprovado -> faturado
            # (faturado = checkout feito; os campos checkout_* registram como,
            # quem e quando).
            conexao.execute(
                """
                CREATE TABLE IF NOT EXISTS orcamentos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paciente_id INTEGER NOT NULL,
                    colaborador_id INTEGER,
                    descricao_itens TEXT NOT NULL,
                    valor_total REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    checkout_forma TEXT,
                    checkout_colaborador_id INTEGER,
                    checkout_em TEXT,
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Elo do checkout: qual orçamento gerou esta cobrança.
            _garantir_coluna(conexao, "cobrancas_boletos", "orcamento_id", "INTEGER")
            # Foto do paciente: nome do arquivo em uploads/fotos_pacientes/.
            _garantir_coluna(conexao, "pacientes", "foto", "TEXT")
        logger.info("Banco de dados pronto em %s", DB_PATH)
    except sqlite3.Error:
        logger.exception("Falha ao inicializar o banco de dados em %s", DB_PATH)
        raise
