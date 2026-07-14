import calendar
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import config
import pdf_fatura
from database import get_connection

logger = logging.getLogger(__name__)

# Mapeamento legado (código -> nome legível). O plano de contas agora vive na
# tabela 'categorias' e as contas guardam o nome direto; isto fica apenas como
# rede de segurança para exibir registros muito antigos que não foram migrados.
CATEGORIAS_LABELS = {
    "fixas": "Contas Fixas",
    "fornecedores": "Fornecedores",
    "utilidades": "Utilidades",
    "impostos": "Impostos",
    "outros": "Outros",
}
FORMAS_PAGAMENTO_LABELS = {
    "outro": "Boleto/Pix",
    "cartao": "Cartão de Crédito",
}
FORMAS_PAGAMENTO_VALIDAS = set(FORMAS_PAGAMENTO_LABELS)

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


class ContaInvalida(Exception):
    """Erro de validação ao criar/alterar uma conta."""


def obter_config(chave):
    with get_connection() as conexao:
        linha = conexao.execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return linha["valor"] if linha else None


def salvar_config(chave, valor):
    with get_connection() as conexao:
        conexao.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, valor),
        )


def listar_destinatarios():
    """Números de WhatsApp cadastrados para receber os relatórios."""
    with get_connection() as conexao:
        linhas = conexao.execute("SELECT id, apelido, numero FROM destinatarios ORDER BY id ASC").fetchall()
    return [{"id": l["id"], "apelido": l["apelido"], "numero": l["numero"]} for l in linhas]


def adicionar_destinatario(apelido, numero):
    import whatsapp  # import local para evitar dependência circular no carregamento

    numero_validado = whatsapp.validar_numero(numero)
    apelido = (apelido or "").strip() or None
    try:
        with get_connection() as conexao:
            conexao.execute(
                "INSERT INTO destinatarios (apelido, numero) VALUES (?, ?)",
                (apelido, numero_validado),
            )
        logger.info("Destinatário '%s' (%s) cadastrado.", apelido or "sem apelido", numero_validado)
    except sqlite3.IntegrityError:
        raise ContaInvalida("Este número já está cadastrado.")
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o destinatário %s", numero_validado)
        raise ContaInvalida("Não foi possível salvar o número no banco de dados.")


def listar_contas_bancarias():
    """Plano de contas: contas de onde sai o dinheiro (ex: Conta Bradesco PF)."""
    with get_connection() as conexao:
        linhas = conexao.execute("SELECT id, nome FROM contas_bancarias ORDER BY nome ASC").fetchall()
    return [{"id": l["id"], "nome": l["nome"]} for l in linhas]


def adicionar_conta_bancaria(nome):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome da conta (ex: Conta Bradesco PF).")
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM contas_bancarias WHERE nome = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe uma conta com este nome no plano de contas.")
            conexao.execute("INSERT INTO contas_bancarias (nome) VALUES (?)", (nome,))
        logger.info("Conta '%s' adicionada ao plano de contas.", nome)
    except sqlite3.Error:
        logger.exception("Falha ao adicionar a conta '%s' ao plano de contas", nome)
        raise ContaInvalida("Não foi possível salvar a conta no plano de contas.")


def remover_conta_bancaria(conta_bancaria_id):
    try:
        with get_connection() as conexao:
            # Lançamentos antigos continuam existindo, apenas sem o vínculo.
            conexao.execute(
                "UPDATE contas SET conta_bancaria_id = NULL WHERE conta_bancaria_id = ?", (conta_bancaria_id,)
            )
            conexao.execute("DELETE FROM contas_bancarias WHERE id = ?", (conta_bancaria_id,))
        logger.info("Conta bancária %s removida do plano de contas.", conta_bancaria_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover a conta bancária %s", conta_bancaria_id)
        raise ContaInvalida("Não foi possível remover a conta do plano de contas.")


def listar_categorias():
    """Plano de contas: categorias disponíveis para classificar os lançamentos."""
    with get_connection() as conexao:
        linhas = conexao.execute("SELECT id, nome FROM categorias ORDER BY nome ASC").fetchall()
    return [{"id": l["id"], "nome": l["nome"]} for l in linhas]


def adicionar_categoria(nome):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome da categoria.")
    try:
        with get_connection() as conexao:
            # Evita duplicados que diferem só em maiúsculas/minúsculas.
            if conexao.execute(
                "SELECT 1 FROM categorias WHERE nome = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe uma categoria com este nome.")
            conexao.execute("INSERT INTO categorias (nome) VALUES (?)", (nome,))
        logger.info("Categoria '%s' adicionada ao plano de contas.", nome)
    except sqlite3.IntegrityError:
        raise ContaInvalida("Já existe uma categoria com este nome.")
    except sqlite3.Error:
        logger.exception("Falha ao adicionar a categoria '%s'", nome)
        raise ContaInvalida("Não foi possível salvar a categoria.")


def remover_categoria(categoria_id):
    try:
        with get_connection() as conexao:
            if conexao.execute("SELECT COUNT(*) AS qtd FROM categorias").fetchone()["qtd"] <= 1:
                raise ContaInvalida("O plano de contas precisa ter pelo menos uma categoria.")
            # As contas já lançadas mantêm o nome da categoria como texto.
            conexao.execute("DELETE FROM categorias WHERE id = ?", (categoria_id,))
        logger.info("Categoria %s removida do plano de contas.", categoria_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover a categoria %s", categoria_id)
        raise ContaInvalida("Não foi possível remover a categoria.")


def listar_cartoes():
    """Cartões de crédito da clínica (ex: Nubank, Bradesco Visa)."""
    with get_connection() as conexao:
        linhas = conexao.execute("SELECT id, nome FROM cartoes ORDER BY nome ASC").fetchall()
    return [{"id": l["id"], "nome": l["nome"]} for l in linhas]


def adicionar_cartao(nome):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome do cartão (ex: Nubank, Bradesco Visa).")
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM cartoes WHERE nome = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe um cartão com este nome.")
            conexao.execute("INSERT INTO cartoes (nome) VALUES (?)", (nome,))
        logger.info("Cartão '%s' cadastrado.", nome)
    except sqlite3.IntegrityError:
        raise ContaInvalida("Já existe um cartão com este nome.")
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o cartão '%s'", nome)
        raise ContaInvalida("Não foi possível salvar o cartão.")


def remover_cartao(cartao_id):
    try:
        with get_connection() as conexao:
            # Lançamentos antigos continuam existindo, apenas sem o vínculo.
            conexao.execute("UPDATE contas SET cartao_id = NULL WHERE cartao_id = ?", (cartao_id,))
            conexao.execute("DELETE FROM cartoes WHERE id = ?", (cartao_id,))
        logger.info("Cartão %s removido.", cartao_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o cartão %s", cartao_id)
        raise ContaInvalida("Não foi possível remover o cartão.")


def listar_fornecedores():
    with get_connection() as conexao:
        linhas = conexao.execute("SELECT id, nome, telefone FROM fornecedores ORDER BY nome ASC").fetchall()
    return [{"id": l["id"], "nome": l["nome"], "telefone": l["telefone"]} for l in linhas]


def adicionar_fornecedor(nome, telefone=None):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome do fornecedor.")
    telefone = (telefone or "").strip() or None
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM fornecedores WHERE nome = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe um fornecedor com este nome.")
            conexao.execute("INSERT INTO fornecedores (nome, telefone) VALUES (?, ?)", (nome, telefone))
        logger.info("Fornecedor '%s' cadastrado.", nome)
    except sqlite3.IntegrityError:
        raise ContaInvalida("Já existe um fornecedor com este nome.")
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o fornecedor '%s'", nome)
        raise ContaInvalida("Não foi possível salvar o fornecedor.")


def remover_fornecedor(fornecedor_id):
    try:
        with get_connection() as conexao:
            # As contas já lançadas continuam existindo, apenas sem o vínculo.
            conexao.execute("UPDATE contas SET fornecedor_id = NULL WHERE fornecedor_id = ?", (fornecedor_id,))
            conexao.execute("DELETE FROM fornecedores WHERE id = ?", (fornecedor_id,))
        logger.info("Fornecedor %s removido.", fornecedor_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o fornecedor %s", fornecedor_id)
        raise ContaInvalida("Não foi possível remover o fornecedor.")


def remover_destinatario(destinatario_id):
    try:
        with get_connection() as conexao:
            conexao.execute("DELETE FROM destinatarios WHERE id = ?", (destinatario_id,))
        logger.info("Destinatário %s removido.", destinatario_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o destinatário %s", destinatario_id)
        raise ContaInvalida("Não foi possível remover o número.")


def formatar_moeda(valor):
    """Formata um número como moeda brasileira: 1234.5 -> '1.234,50'."""
    inteiro, decimal = f"{valor:,.2f}".split(".")
    inteiro = inteiro.replace(",", ".")
    return f"{inteiro},{decimal}"


def _formatar_data_br(vencimento_iso):
    ano, mes, dia = vencimento_iso.split("-")
    return f"{dia}/{mes}/{ano}"


def _status_exibicao(status, vencimento_iso, hoje):
    if status == "pago":
        return "pago"
    if vencimento_iso < hoje.isoformat():
        return "vencido"
    return "pendente"


def _referencia_fatura(vencimento_iso):
    """A fatura de um lançamento no cartão é identificada pelo mês/ano do vencimento."""
    return vencimento_iso[:7]  # 'YYYY-MM'


def _label_fatura(referencia):
    ano, mes = referencia.split("-")
    return f"{MESES_PT[int(mes)]}/{ano}"


def listar_contas():
    hoje = date.today()
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT contas.*, fornecedores.nome AS fornecedor_nome, contas_bancarias.nome AS conta_bancaria_nome, "
            "alertas_preco.percentual AS alerta_percentual, alertas_preco.media_historica AS alerta_media "
            "FROM contas "
            "LEFT JOIN fornecedores ON fornecedores.id = contas.fornecedor_id "
            "LEFT JOIN contas_bancarias ON contas_bancarias.id = contas.conta_bancaria_id "
            "LEFT JOIN alertas_preco ON alertas_preco.conta_id = contas.id "
            "ORDER BY contas.vencimento ASC, contas.id ASC"
        ).fetchall()

    return [
        {
            "id": linha["id"],
            "nome": linha["nome"],
            "valor": linha["valor"],
            "vencimento": _formatar_data_br(linha["vencimento"]),
            "categoria": CATEGORIAS_LABELS.get(linha["categoria"], linha["categoria"]),
            "status": _status_exibicao(linha["status"], linha["vencimento"], hoje),
            "forma_pagamento": linha["forma_pagamento"],
            "forma_pagamento_label": FORMAS_PAGAMENTO_LABELS.get(linha["forma_pagamento"], linha["forma_pagamento"]),
            "fatura_label": _label_fatura(linha["fatura_referencia"]) if linha["fatura_referencia"] else None,
            "fornecedor": linha["fornecedor_nome"],
            "conta_bancaria": linha["conta_bancaria_nome"],
            "comprovante": linha["comprovante"],
            "alerta_preco": (
                {"percentual": linha["alerta_percentual"], "media": linha["alerta_media"]}
                if linha["alerta_percentual"] is not None
                else None
            ),
        }
        for linha in linhas
    ]


def salvar_comprovante(conta_id, nome_arquivo):
    """Registra o arquivo do comprovante de pagamento na conta."""
    try:
        with get_connection() as conexao:
            linha = conexao.execute("SELECT id FROM contas WHERE id = ?", (conta_id,)).fetchone()
            if linha is None:
                raise ContaInvalida("Conta não encontrada.")
            conexao.execute("UPDATE contas SET comprovante = ? WHERE id = ?", (nome_arquivo, conta_id))
        logger.info("Comprovante '%s' anexado à conta %s.", nome_arquivo, conta_id)
    except sqlite3.Error:
        logger.exception("Falha ao salvar o comprovante da conta %s", conta_id)
        raise ContaInvalida("Não foi possível salvar o comprovante.")


def obter_comprovante(conta_id):
    """Nome do arquivo do comprovante da conta (ou None)."""
    with get_connection() as conexao:
        linha = conexao.execute("SELECT comprovante FROM contas WHERE id = ?", (conta_id,)).fetchone()
    return linha["comprovante"] if linha else None


def _somar_meses(data, meses):
    """Avança a data em N meses mantendo o dia (ajusta para o último dia do mês
    quando necessário, ex: 31/01 + 1 mês = 28/02)."""
    mes_zero = data.month - 1 + meses
    ano = data.year + mes_zero // 12
    mes = mes_zero % 12 + 1
    dia = min(data.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def _dividir_em_parcelas(valor_total, parcelas):
    """Divide o total em parcelas iguais em centavos; a última leva a sobra
    para a soma bater exatamente com o valor da compra."""
    total_centavos = round(valor_total * 100)
    base = total_centavos // parcelas
    valores = [base] * parcelas
    valores[-1] += total_centavos - base * parcelas
    return [v / 100 for v in valores]


# Uma conta nova mais de 10% acima da média histórica gera o alerta 📈.
LIMITE_INFLACAO_PERCENTUAL = 10.0


def _media_historica_preco(conexao, nome, fornecedor_id, vencimento_iso):
    """Média de valor das contas dos 3 meses anteriores ao vencimento informado,
    casando pelo mesmo nome (incluindo parcelas/recorrências "Nome (…)") ou pelo
    mesmo fornecedor. Devolve None quando não há histórico para comparar."""
    inicio_janela = _somar_meses(datetime.strptime(vencimento_iso, "%Y-%m-%d").date(), -3).isoformat()
    # Escapa curingas do LIKE para nomes com % ou _.
    nome_escapado = nome.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    filtro_fornecedor = "OR fornecedor_id = ?" if fornecedor_id is not None else ""
    parametros = [nome, f"{nome_escapado} (%"]
    if fornecedor_id is not None:
        parametros.append(fornecedor_id)
    parametros += [inicio_janela, vencimento_iso]

    linha = conexao.execute(
        "SELECT AVG(valor) AS media, COUNT(*) AS qtd FROM contas "
        f"WHERE (nome = ? COLLATE NOCASE OR nome LIKE ? ESCAPE '\\' COLLATE NOCASE {filtro_fornecedor}) "
        "AND vencimento >= ? AND vencimento < ?",
        parametros,
    ).fetchone()
    return linha["media"] if linha and linha["qtd"] else None


def _registrar_alerta_preco(conexao, conta_id, valor_novo, media):
    percentual = (valor_novo - media) / media * 100
    conexao.execute(
        "INSERT OR REPLACE INTO alertas_preco (conta_id, valor_novo, media_historica, percentual) "
        "VALUES (?, ?, ?, ?)",
        (conta_id, valor_novo, media, round(percentual, 1)),
    )
    logger.warning(
        "📈 Inflação de preço na conta %s: R$ %s está %.1f%% acima da média histórica de R$ %s.",
        conta_id, formatar_moeda(valor_novo), percentual, formatar_moeda(media),
    )


def criar_conta(
    nome, valor, vencimento, categoria, forma_pagamento="outro",
    fornecedor_id=None, parcelas=1, conta_bancaria_id=None, recorrencia=1,
    ja_paga=False, cartao_id=None,
):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome da conta.")

    try:
        valor = float(valor)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe um valor numérico válido.")
    if valor <= 0:
        raise ContaInvalida("O valor deve ser maior que zero.")

    try:
        primeiro_vencimento = datetime.strptime(vencimento, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ContaInvalida("Informe uma data de vencimento válida.")

    categoria = (categoria or "").strip()
    with get_connection() as conexao:
        if conexao.execute("SELECT 1 FROM categorias WHERE nome = ?", (categoria,)).fetchone() is None:
            raise ContaInvalida("Categoria inválida. Cadastre-a no Plano de Contas (aba Cadastros).")

    if forma_pagamento not in FORMAS_PAGAMENTO_VALIDAS:
        raise ContaInvalida("Forma de pagamento inválida.")

    try:
        parcelas = int(parcelas or 1)
    except (TypeError, ValueError):
        raise ContaInvalida("Número de parcelas inválido.")
    if not 1 <= parcelas <= 48:
        raise ContaInvalida("O parcelamento deve ter entre 1 e 48 parcelas.")

    try:
        recorrencia = int(recorrencia or 1)
    except (TypeError, ValueError):
        raise ContaInvalida("Recorrência inválida.")
    if not 1 <= recorrencia <= 48:
        raise ContaInvalida("A recorrência deve ter entre 2 e 48 meses.")
    if parcelas > 1 and recorrencia > 1:
        raise ContaInvalida(
            "Uma conta não pode ser parcelada e recorrente ao mesmo tempo. "
            "Parcelamento divide o valor total; recorrência repete o valor todo mês."
        )

    if fornecedor_id in (None, "", "0"):
        fornecedor_id = None
    else:
        try:
            fornecedor_id = int(fornecedor_id)
        except (TypeError, ValueError):
            raise ContaInvalida("Fornecedor inválido.")
        with get_connection() as conexao:
            if conexao.execute("SELECT 1 FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone() is None:
                raise ContaInvalida("Fornecedor não encontrado.")

    if conta_bancaria_id in (None, "", "0"):
        conta_bancaria_id = None
    else:
        try:
            conta_bancaria_id = int(conta_bancaria_id)
        except (TypeError, ValueError):
            raise ContaInvalida("Conta do plano de contas inválida.")
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM contas_bancarias WHERE id = ?", (conta_bancaria_id,)
            ).fetchone() is None:
                raise ContaInvalida("Conta do plano de contas não encontrada.")

    if cartao_id in (None, "", "0"):
        cartao_id = None
    else:
        try:
            cartao_id = int(cartao_id)
        except (TypeError, ValueError):
            raise ContaInvalida("Cartão inválido.")
        with get_connection() as conexao:
            if conexao.execute("SELECT 1 FROM cartoes WHERE id = ?", (cartao_id,)).fetchone() is None:
                raise ContaInvalida("Cartão não encontrado. Cadastre-o na aba Cadastros.")

    # Uma compra parcelada vira N contas, uma por mês, com o valor dividido.
    # Uma conta recorrente também vira N contas mensais, mas o valor cheio se
    # repete em cada mês (ex: aluguel de R$ 2.000 por 12 meses).
    if recorrencia > 1:
        valores = [valor] * recorrencia
        grupo = str(uuid.uuid4())
    else:
        valores = _dividir_em_parcelas(valor, parcelas)
        grupo = str(uuid.uuid4()) if parcelas > 1 else None

    try:
        with get_connection() as conexao:
            # Monitoramento de preços: compara a 1ª parcela/ocorrência com a média
            # dos 3 meses anteriores ANTES de inserir, para o próprio lote
            # (parcelas/recorrências) não entrar na conta da média.
            try:
                media_historica = _media_historica_preco(
                    conexao, nome, fornecedor_id, primeiro_vencimento.isoformat()
                )
            except sqlite3.Error:
                logger.exception("Falha ao calcular a média histórica de preço de '%s'", nome)
                media_historica = None

            primeira_conta_id = None
            for indice, valor_parcela in enumerate(valores):
                venc_parcela = _somar_meses(primeiro_vencimento, indice)
                if parcelas > 1:
                    nome_parcela = f"{nome} ({indice + 1}/{parcelas})"
                elif recorrencia > 1:
                    nome_parcela = f"{nome} ({_label_fatura(_referencia_fatura(venc_parcela.isoformat()))})"
                else:
                    nome_parcela = nome
                fatura_referencia = (
                    _referencia_fatura(venc_parcela.isoformat()) if forma_pagamento == "cartao" else None
                )
                cursor = conexao.execute(
                    "INSERT INTO contas (nome, valor, vencimento, categoria, status, pago_em, "
                    "forma_pagamento, fatura_referencia, fornecedor_id, grupo_parcelamento, "
                    "conta_bancaria_id, cartao_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        nome_parcela,
                        valor_parcela,
                        venc_parcela.isoformat(),
                        categoria,
                        # Importação de extrato entra como paga: o dinheiro já saiu do banco.
                        "pago" if ja_paga else "pendente",
                        datetime.now().isoformat(timespec="seconds") if ja_paga else None,
                        forma_pagamento,
                        fatura_referencia,
                        fornecedor_id,
                        grupo,
                        conta_bancaria_id,
                        cartao_id,
                    ),
                )
                if primeira_conta_id is None:
                    primeira_conta_id = cursor.lastrowid
                if fatura_referencia:
                    conexao.execute(
                        "INSERT OR IGNORE INTO faturas (referencia, status) VALUES (?, 'aberta')",
                        (fatura_referencia,),
                    )

            if (
                media_historica
                and valores[0] > media_historica * (1 + LIMITE_INFLACAO_PERCENTUAL / 100)
            ):
                try:
                    _registrar_alerta_preco(conexao, primeira_conta_id, valores[0], media_historica)
                except sqlite3.Error:
                    # O alerta é um extra: falhar aqui não pode impedir o cadastro.
                    logger.exception("Falha ao gravar o alerta de preço da conta '%s'", nome)
        if recorrencia > 1:
            detalhe = f" recorrente por {recorrencia} meses"
        elif parcelas > 1:
            detalhe = f" em {parcelas}x"
        else:
            detalhe = ""
        logger.info(
            "Conta '%s' cadastrada (R$ %s%s, 1º vencimento %s, forma: %s).",
            nome,
            formatar_moeda(valor),
            detalhe,
            primeiro_vencimento.isoformat(),
            forma_pagamento,
        )
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar a conta '%s'", nome)
        raise ContaInvalida("Não foi possível salvar a conta no banco de dados.")


def conta_ja_existe(nome, valor, vencimento_iso):
    """Existe uma conta com este nome, valor e vencimento? Usado pela importação
    de extrato para marcar transações que já viraram conta no sistema."""
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return False
    with get_connection() as conexao:
        return conexao.execute(
            "SELECT 1 FROM contas WHERE nome = ? COLLATE NOCASE AND vencimento = ? "
            "AND ABS(valor - ?) < 0.005",
            ((nome or "").strip(), vencimento_iso, valor),
        ).fetchone() is not None


def alternar_status(conta_id):
    try:
        with get_connection() as conexao:
            linha = conexao.execute("SELECT status FROM contas WHERE id = ?", (conta_id,)).fetchone()
            if linha is None:
                raise ContaInvalida("Conta não encontrada.")

            novo_status = "pendente" if linha["status"] == "pago" else "pago"
            pago_em = datetime.now().isoformat(timespec="seconds") if novo_status == "pago" else None
            conexao.execute(
                "UPDATE contas SET status = ?, pago_em = ? WHERE id = ?",
                (novo_status, pago_em, conta_id),
            )
        logger.info("Conta %s marcada como '%s'.", conta_id, novo_status)
    except sqlite3.Error:
        logger.exception("Falha ao alternar status da conta %s", conta_id)
        raise ContaInvalida("Não foi possível atualizar o status da conta.")


def excluir_conta(conta_id):
    try:
        with get_connection() as conexao:
            conexao.execute("DELETE FROM alertas_preco WHERE conta_id = ?", (conta_id,))
            conexao.execute("DELETE FROM contas WHERE id = ?", (conta_id,))
        logger.info("Conta %s excluída.", conta_id)
    except sqlite3.Error:
        logger.exception("Falha ao excluir a conta %s", conta_id)
        raise ContaInvalida("Não foi possível excluir a conta.")


# ==========================================================================
# Gestão e Cobrança de Boletos (boletos emitidos para clientes da clínica)
# ==========================================================================


def _normalizar_telefone_cliente(telefone):
    """Aceita o telefone como o usuário digita ("(11) 99999-9999") e devolve no
    formato internacional usado pelo WhatsApp (+5511999999999)."""
    import whatsapp  # import local para evitar dependência circular no carregamento

    bruto = (telefone or "").strip()
    if not bruto:
        raise ContaInvalida("Informe o telefone do cliente com DDD.")
    if not bruto.startswith("+"):
        digitos = re.sub(r"\D", "", bruto)
        # 10 ou 11 dígitos = DDD + número local; completa o +55 do Brasil.
        bruto = f"+55{digitos}" if len(digitos) in (10, 11) else f"+{digitos}"
    try:
        return whatsapp.validar_numero(bruto)
    except whatsapp.EnvioWhatsAppInvalido:
        raise ContaInvalida("Telefone inválido. Digite com DDD, ex: (11) 99999-9999.")


def calcular_risco_clientes(linhas, hoje):
    """Termômetro de Risco: mede, por cliente, a média de dias de atraso dos
    boletos dele. Contam como atraso os boletos pagos depois do vencimento
    (data_pagamento - data_vencimento) e os vencidos ainda em aberto (dias
    correndo até hoje). Boletos a vencer e pagos antes da coluna data_pagamento
    existir ficam de fora, pois não há como medir o atraso deles.

    Devolve {nome normalizado: {"nivel", "label", "tooltip"}} com os níveis:
    média 0 = baixo (verde), até 7 dias = médio (amarelo), acima = alto (vermelho).
    """
    historico = {}  # nome normalizado -> lista de atrasos (em dias) medidos
    for linha in linhas:
        chave = linha["nome_cliente"].strip().lower()
        atrasos = historico.setdefault(chave, [])
        vencimento = date.fromisoformat(linha["data_vencimento"])
        if linha["status"] == "pago" and linha["data_pagamento"]:
            atrasos.append(max(0, (date.fromisoformat(linha["data_pagamento"]) - vencimento).days))
        elif linha["status"] != "pago" and vencimento < hoje:
            atrasos.append((hoje - vencimento).days)

    riscos = {}
    for chave, atrasos in historico.items():
        media = sum(atrasos) / len(atrasos) if atrasos else 0.0
        media_br = f"{media:.1f}".replace(".", ",").removesuffix(",0")
        if media == 0:
            nivel, label = "baixo", "Baixo Risco"
            tooltip = (
                f"Pagou {len(atrasos)} boleto(s) sem nenhum dia de atraso."
                if atrasos
                else "Cliente sem histórico de atrasos até agora."
            )
        elif media <= 7:
            nivel, label = "medio", "Médio Risco"
            tooltip = f"Média de {media_br} dia(s) de atraso em {len(atrasos)} boleto(s) analisado(s)."
        else:
            nivel, label = "alto", "Alto Risco"
            tooltip = f"Média de {media_br} dia(s) de atraso em {len(atrasos)} boleto(s) analisado(s)."
        riscos[chave] = {"nivel": nivel, "label": label, "tooltip": tooltip}
    return riscos


def listar_boletos():
    hoje = date.today()
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM cobrancas_boletos ORDER BY data_vencimento ASC, id ASC"
        ).fetchall()

    riscos = calcular_risco_clientes(linhas, hoje)

    # Boletos são contas A RECEBER (clientes devendo à clínica); os rótulos
    # deixam a direção do dinheiro explícita na tela.
    status_labels = {"pago": "Recebido", "pendente": "A Receber", "vencido": "Vencido"}

    boletos = []
    for linha in linhas:
        status = _status_exibicao(linha["status"], linha["data_vencimento"], hoje)
        ultima = linha["ultima_notificacao"]
        if ultima == hoje.isoformat():
            ultimo_aviso = "Hoje"
        elif ultima:
            ultimo_aviso = _formatar_data_br(ultima)
        else:
            ultimo_aviso = None
        boletos.append(
            {
                "id": linha["id"],
                "nome_cliente": linha["nome_cliente"],
                "telefone": linha["telefone"],
                "valor": linha["valor"],
                "vencimento": _formatar_data_br(linha["data_vencimento"]),
                "status": status,
                "status_label": status_labels[status],
                "dias_atraso": (
                    (hoje - date.fromisoformat(linha["data_vencimento"])).days
                    if status == "vencido"
                    else 0
                ),
                "ultimo_aviso": ultimo_aviso,
                "risco": riscos[linha["nome_cliente"].strip().lower()],
                "mensagem_agendada_data": linha["mensagem_agendada_data"],
                "mensagem_agendada_br": (
                    _formatar_data_br(linha["mensagem_agendada_data"])
                    if linha["mensagem_agendada_data"]
                    else None
                ),
                "mensagem_agendada_texto": linha["mensagem_agendada_texto"] or "",
            }
        )
    return boletos


def criar_boleto(nome_cliente, telefone, valor, data_vencimento):
    nome_cliente = (nome_cliente or "").strip()
    if not nome_cliente:
        raise ContaInvalida("Informe o nome do cliente.")

    telefone = _normalizar_telefone_cliente(telefone)

    try:
        valor = float(valor)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe um valor numérico válido.")
    if valor <= 0:
        raise ContaInvalida("O valor deve ser maior que zero.")

    try:
        vencimento = datetime.strptime(data_vencimento, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ContaInvalida("Informe uma data de vencimento válida.")

    try:
        with get_connection() as conexao:
            conexao.execute(
                "INSERT INTO cobrancas_boletos (nome_cliente, telefone, valor, data_vencimento, status) "
                "VALUES (?, ?, ?, ?, 'pendente')",
                (nome_cliente, telefone, valor, vencimento.isoformat()),
            )
        logger.info(
            "Boleto de '%s' cadastrado (R$ %s, vencimento %s).",
            nome_cliente, formatar_moeda(valor), vencimento.isoformat(),
        )
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o boleto de '%s'", nome_cliente)
        raise ContaInvalida("Não foi possível salvar o boleto no banco de dados.")


def alternar_status_boleto(boleto_id):
    try:
        with get_connection() as conexao:
            linha = conexao.execute(
                "SELECT status FROM cobrancas_boletos WHERE id = ?", (boleto_id,)
            ).fetchone()
            if linha is None:
                raise ContaInvalida("Boleto não encontrado.")

            novo_status = "pendente" if linha["status"] == "pago" else "pago"
            # A data de pagamento alimenta o Termômetro de Risco; desmarcar o
            # boleto (volta a pendente) apaga a data para não distorcer a média.
            data_pagamento = date.today().isoformat() if novo_status == "pago" else None
            conexao.execute(
                "UPDATE cobrancas_boletos SET status = ?, data_pagamento = ? WHERE id = ?",
                (novo_status, data_pagamento, boleto_id),
            )
        logger.info("Boleto %s marcado como '%s'.", boleto_id, novo_status)
    except sqlite3.Error:
        logger.exception("Falha ao alternar status do boleto %s", boleto_id)
        raise ContaInvalida("Não foi possível atualizar o status do boleto.")


def excluir_boleto(boleto_id):
    try:
        with get_connection() as conexao:
            conexao.execute("DELETE FROM cobrancas_boletos WHERE id = ?", (boleto_id,))
        logger.info("Boleto %s excluído.", boleto_id)
    except sqlite3.Error:
        logger.exception("Falha ao excluir o boleto %s", boleto_id)
        raise ContaInvalida("Não foi possível excluir o boleto.")


# Régua de cobrança: padrões dos dois lados da régua (o usuário ajusta na tela
# de boletos, fica em 'configuracoes'). Lembrete = dias ANTES do vencimento;
# intervalo = de quantos em quantos dias a cobrança repete DEPOIS de vencer.
DIAS_LEMBRETE_PREVIO = 3
DIAS_INTERVALO_COBRANCA = 1
DIAS_REGUA_MIN, DIAS_REGUA_MAX = 1, 30

CENARIOS_REGUA = {
    "vencido": "Cobrança (vencido)",
    "vence_hoje": "Alerta do dia (vence hoje)",
    "lembrete": "Lembrete prévio",
}


def _obter_dias_config(chave, padrao):
    valor = obter_config(chave)
    try:
        dias = int(valor)
    except (TypeError, ValueError):
        return padrao
    if not DIAS_REGUA_MIN <= dias <= DIAS_REGUA_MAX:
        return padrao
    return dias


def obter_dias_lembrete():
    """Dias antes do vencimento em que o lembrete prévio é enviado (configurável)."""
    return _obter_dias_config("dias_lembrete_previo", DIAS_LEMBRETE_PREVIO)


def obter_intervalo_cobranca():
    """De quantos em quantos dias a cobrança de vencidos repete (1 = todos os dias)."""
    return _obter_dias_config("dias_intervalo_cobranca", DIAS_INTERVALO_COBRANCA)


def _validar_dias_regua(valor, descricao):
    try:
        dias = int(valor)
    except (TypeError, ValueError):
        raise ContaInvalida(f"Informe {descricao} em número inteiro de dias.")
    if not DIAS_REGUA_MIN <= dias <= DIAS_REGUA_MAX:
        raise ContaInvalida(
            f"O valor de {descricao} deve ficar entre {DIAS_REGUA_MIN} e {DIAS_REGUA_MAX} dias."
        )
    return dias


def salvar_config_regua(dias_lembrete, intervalo_cobranca):
    """Valida e salva os dois lados da régua juntos (nada é gravado se um for inválido)."""
    lembrete = _validar_dias_regua(dias_lembrete, "o lembrete antes do vencimento")
    intervalo = _validar_dias_regua(intervalo_cobranca, "o intervalo de cobrança após o vencimento")
    salvar_config("dias_lembrete_previo", str(lembrete))
    salvar_config("dias_intervalo_cobranca", str(intervalo))
    logger.info(
        "Régua de cobrança ajustada: lembrete %d dia(s) antes; cobrança a cada %d dia(s) após vencer.",
        lembrete, intervalo,
    )
    return lembrete, intervalo


# Meta de Arrecadação Mensal: quanto o usuário quer receber de boletos no mês,
# salvo em 'configuracoes'. Meta zero ou vazia = desligada (a barra some da tela).


def obter_meta_mensal():
    valor = obter_config("meta_arrecadacao_mensal")
    try:
        meta = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return meta if meta > 0 else 0.0


def salvar_meta_mensal(valor):
    """Valida e salva a meta do mês. Campo vazio ou zero desliga a meta."""
    bruto = (valor or "").strip()
    if not bruto:
        salvar_config("meta_arrecadacao_mensal", "0")
        logger.info("Meta de arrecadação mensal desligada.")
        return 0.0
    try:
        meta = float(bruto)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe um valor numérico válido para a meta.")
    if meta < 0:
        raise ContaInvalida("A meta não pode ser negativa.")
    salvar_config("meta_arrecadacao_mensal", f"{meta:.2f}")
    logger.info("Meta de arrecadação mensal ajustada para R$ %s.", formatar_moeda(meta))
    return meta


def montar_progresso_meta(recebido, mes_label):
    """Dados da barra 'Meta de Arrecadação Mensal' da tela de boletos: percentual
    do recebido no mês sobre a meta e a frase motivacional correspondente.
    Devolve None quando a meta está desligada (a tela esconde a barra)."""
    meta = obter_meta_mensal()
    if meta <= 0:
        return None

    percentual = recebido / meta * 100
    if percentual >= 100:
        tom = "atingida"
        frase = f"Meta de {mes_label} atingida! Parabéns, excelente mês para a {config.NOME_CLINICA}! 🎉"
    elif percentual >= 50:
        tom = "bom"
        frase = (
            f"Você já passou da metade! Faltam R$ {formatar_moeda(meta - recebido)} "
            "para bater a meta — continue firme. 🚀"
        )
    else:
        tom = "inicio"
        frase = f"Vamos com tudo! Cada boleto recebido aproxima você da meta de {mes_label}. 💪"

    return {
        "meta": meta,
        "recebido": recebido,
        "percentual_label": f"{percentual:.1f}".replace(".", ",").removesuffix(",0"),
        "largura": min(percentual, 100),
        "tom": tom,
        "frase": frase,
    }


def listar_regua_cobranca():
    """Boletos pendentes que devem receber mensagem hoje, classificados por cenário:
    'vencido' (1+ dias de atraso, cobrado de N em N dias — intervalo configurável),
    'vence_hoje' e 'lembrete' (vence daqui a exatos N dias — configurável).
    Boletos já avisados hoje (ultima_notificacao) ficam de fora — é a trava
    contra mensagens duplicadas no mesmo dia."""
    hoje_data = date.today()
    hoje = hoje_data.isoformat()
    data_lembrete = (hoje_data + timedelta(days=obter_dias_lembrete())).isoformat()
    # Um vencido só volta a ser cobrado quando o último aviso (de qualquer
    # cenário) tiver pelo menos o intervalo configurado de idade.
    limite_cobranca = (hoje_data - timedelta(days=obter_intervalo_cobranca() - 1)).isoformat()

    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM cobrancas_boletos WHERE status = 'pendente' "
            "AND (data_vencimento <= ? OR data_vencimento = ?) "
            "AND (ultima_notificacao IS NULL OR ultima_notificacao < ?) "
            "ORDER BY data_vencimento ASC, id ASC",
            (hoje, data_lembrete, hoje),
        ).fetchall()

    regua = []
    for linha in linhas:
        boleto = dict(linha)
        if boleto["data_vencimento"] < hoje:
            if boleto["ultima_notificacao"] and boleto["ultima_notificacao"] >= limite_cobranca:
                continue  # cobrado há menos dias que o intervalo escolhido
            boleto["cenario"] = "vencido"
        elif boleto["data_vencimento"] == hoje:
            boleto["cenario"] = "vence_hoje"
        else:
            boleto["cenario"] = "lembrete"
        regua.append(boleto)
    return regua


def _mensagem_regua(boleto):
    """Texto da mensagem de cada cenário da régua, sempre amigável e personalizado."""
    hoje = date.today()
    dias = (date.fromisoformat(boleto["data_vencimento"]) - hoje).days
    vencimento_br = _formatar_data_br(boleto["data_vencimento"])
    primeiro_nome = boleto["nome_cliente"].strip().split()[0]
    valor = formatar_moeda(boleto["valor"])

    if dias > 0:
        # Lembrete prévio: só avisa que está chegando, sem tom de cobrança.
        prazo = "amanhã" if dias == 1 else f"daqui a {dias} dias"
        return (
            f"Olá, {primeiro_nome}! 😊 Tudo bem?\n\n"
            f"Aqui é da *{config.NOME_CLINICA}*. Passando para lembrar que o seu boleto de "
            f"*R$ {valor}* vence {prazo}, em *{vencimento_br}*.\n\n"
            f"Assim dá para se programar com calma! Se precisar da segunda via, "
            f"é só responder por aqui. 💚"
        )

    if dias == 0:
        return (
            f"Olá, {primeiro_nome}! 😊 Tudo bem?\n\n"
            f"Aqui é da *{config.NOME_CLINICA}*. Lembrete rápido: o seu boleto de *R$ {valor}* "
            f"*vence hoje* ({vencimento_br}).\n\n"
            f"Se o pagamento já foi feito, por favor desconsidere esta mensagem. 💚\n\n"
            f"Precisando da segunda via ou de qualquer ajuda, é só responder por aqui!"
        )

    atraso = -dias
    situacao = f"venceu ontem ({vencimento_br})" if atraso == 1 else f"venceu em {vencimento_br}, há {atraso} dias"
    return (
        f"Olá, {primeiro_nome}! 😊 Tudo bem?\n\n"
        f"Aqui é da *{config.NOME_CLINICA}*. Passando só para lembrar que o boleto no valor de "
        f"*R$ {valor}* {situacao}.\n\n"
        f"Se o pagamento já foi feito, por favor desconsidere esta mensagem. 💚\n\n"
        f"Precisando da segunda via ou de qualquer ajuda, é só responder por aqui. Obrigado!"
    )


def _marcar_notificado_hoje(boleto_id):
    with get_connection() as conexao:
        conexao.execute(
            "UPDATE cobrancas_boletos SET ultima_notificacao = ? WHERE id = ?",
            (date.today().isoformat(), boleto_id),
        )


def _enviar_notificacoes_boletos(boletos, intervalo=None):
    """Envia a mensagem da régua para cada boleto da lista, com pausa entre envios.

    Cada envio bem-sucedido grava a data em ultima_notificacao (um envio que
    falha NÃO grava, então entra de novo na varredura seguinte). Devolve uma
    lista de (nome_cliente, cenario, sucesso, erro)."""
    import time

    import whatsapp  # import local para evitar dependência circular no carregamento

    if intervalo is None:
        intervalo = whatsapp.INTERVALO_ENTRE_ENVIOS

    resultados = []
    for indice, boleto in enumerate(boletos):
        if indice > 0:
            time.sleep(intervalo)
        try:
            whatsapp.enviar_mensagem(boleto["telefone"], _mensagem_regua(boleto))
            _marcar_notificado_hoje(boleto["id"])
            logger.info(
                "%s enviado para %s (%s, R$ %s).",
                CENARIOS_REGUA[boleto["cenario"]], boleto["nome_cliente"],
                boleto["telefone"], formatar_moeda(boleto["valor"]),
            )
            resultados.append((boleto["nome_cliente"], boleto["cenario"], True, None))
        except whatsapp.EnvioWhatsAppInvalido as erro:
            logger.error(
                "%s para %s falhou: %s",
                CENARIOS_REGUA[boleto["cenario"]], boleto["nome_cliente"], erro,
            )
            resultados.append((boleto["nome_cliente"], boleto["cenario"], False, str(erro)))
    return resultados


def enviar_regua_cobranca(intervalo=None):
    """Roda a régua de cobrança completa do dia: lembretes prévios (3 dias antes),
    alertas de "vence hoje" e cobranças de vencidos, sem repetir quem já foi
    avisado hoje. Devolve a lista de resultados de _enviar_notificacoes_boletos."""
    boletos = listar_regua_cobranca()
    if not boletos:
        logger.info("Régua de cobrança: nenhum boleto para avisar hoje.")
        return []

    por_cenario = {c: sum(1 for b in boletos if b["cenario"] == c) for c in CENARIOS_REGUA}
    logger.info(
        "Régua de cobrança: %d mensagem(ns) — %d cobrança(s), %d vencendo hoje, %d lembrete(s).",
        len(boletos), por_cenario["vencido"], por_cenario["vence_hoje"], por_cenario["lembrete"],
    )
    resultados = _enviar_notificacoes_boletos(boletos, intervalo)
    sucessos = sum(1 for _, _, ok, _ in resultados if ok)
    logger.info("Régua de cobrança finalizada: %d de %d mensagens enviadas.", sucessos, len(boletos))
    return resultados


def verificar_e_cobrar_boletos_vencidos(intervalo=None):
    """Envia agora só o cenário 'vencido' da régua (usado pelo botão da tela).

    Boletos que já receberam mensagem hoje ficam de fora, como em toda a régua."""
    boletos = [b for b in listar_regua_cobranca() if b["cenario"] == "vencido"]
    if not boletos:
        logger.info("Cobrança manual: nenhum boleto vencido sem aviso hoje.")
        return []
    logger.info("Cobrança manual: %d boleto(s) vencido(s) a cobrar.", len(boletos))
    return _enviar_notificacoes_boletos(boletos, intervalo)


# ==========================================================================
# Agendamento de Mensagem Customizada: recado avulso marcado pelo usuário na
# tela de boletos, disparado pelo envio diário na data escolhida.
# ==========================================================================

TAMANHO_MAX_RECADO = 1000


def agendar_mensagem_boleto(boleto_id, data, texto):
    """Valida e grava o recado customizado do boleto (data futura ou hoje)."""
    texto = (texto or "").strip()
    if not texto:
        raise ContaInvalida("Digite o recado que será enviado ao cliente.")
    if len(texto) > TAMANHO_MAX_RECADO:
        raise ContaInvalida(f"O recado pode ter no máximo {TAMANHO_MAX_RECADO} caracteres.")

    try:
        data_envio = datetime.strptime(data or "", "%Y-%m-%d").date()
    except ValueError:
        raise ContaInvalida("Escolha uma data válida para o envio do recado.")
    if data_envio < date.today():
        raise ContaInvalida("A data do recado não pode ficar no passado.")

    try:
        with get_connection() as conexao:
            linha = conexao.execute(
                "SELECT nome_cliente FROM cobrancas_boletos WHERE id = ?", (boleto_id,)
            ).fetchone()
            if linha is None:
                raise ContaInvalida("Boleto não encontrado.")
            conexao.execute(
                "UPDATE cobrancas_boletos "
                "SET mensagem_agendada_data = ?, mensagem_agendada_texto = ? WHERE id = ?",
                (data_envio.isoformat(), texto, boleto_id),
            )
        logger.info(
            "Recado agendado para '%s' em %s (boleto %s).",
            linha["nome_cliente"], data_envio.isoformat(), boleto_id,
        )
        return linha["nome_cliente"], data_envio
    except sqlite3.Error:
        logger.exception("Falha ao agendar recado do boleto %s", boleto_id)
        raise ContaInvalida("Não foi possível salvar o recado agendado.")


def cancelar_mensagem_boleto(boleto_id):
    """Remove o recado agendado do boleto (o envio na data marcada não acontece)."""
    try:
        with get_connection() as conexao:
            conexao.execute(
                "UPDATE cobrancas_boletos "
                "SET mensagem_agendada_data = NULL, mensagem_agendada_texto = NULL WHERE id = ?",
                (boleto_id,),
            )
        logger.info("Recado agendado do boleto %s removido.", boleto_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o recado agendado do boleto %s", boleto_id)
        raise ContaInvalida("Não foi possível remover o recado agendado.")


def listar_mensagens_agendadas():
    """Boletos com recado marcado para hoje ou para uma data que já passou —
    se o computador ficou desligado no dia, o recado sai na próxima varredura."""
    hoje = date.today().isoformat()
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM cobrancas_boletos "
            "WHERE mensagem_agendada_data IS NOT NULL AND mensagem_agendada_data <= ? "
            "AND mensagem_agendada_texto IS NOT NULL "
            "ORDER BY mensagem_agendada_data ASC, id ASC",
            (hoje,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def enviar_mensagens_agendadas(intervalo=None):
    """Dispara os recados customizados do dia pelo WhatsApp, com pausa entre envios.

    O recado é enviado exatamente como o usuário digitou. Envio bem-sucedido
    limpa o agendamento (é um disparo único); envio que falha mantém tudo para
    tentar de novo na próxima varredura. Devolve (nome_cliente, sucesso, erro)."""
    import time

    import whatsapp  # import local para evitar dependência circular no carregamento

    boletos = listar_mensagens_agendadas()
    if not boletos:
        logger.info("Recados agendados: nenhum para enviar hoje.")
        return []

    if intervalo is None:
        intervalo = whatsapp.INTERVALO_ENTRE_ENVIOS

    logger.info("Recados agendados: %d envio(s) para hoje.", len(boletos))
    resultados = []
    for indice, boleto in enumerate(boletos):
        if indice > 0:
            time.sleep(intervalo)
        try:
            whatsapp.enviar_mensagem(boleto["telefone"], boleto["mensagem_agendada_texto"])
            cancelar_mensagem_boleto(boleto["id"])
            logger.info(
                "Recado agendado enviado para %s (%s).",
                boleto["nome_cliente"], boleto["telefone"],
            )
            resultados.append((boleto["nome_cliente"], True, None))
        except whatsapp.EnvioWhatsAppInvalido as erro:
            logger.error("Recado agendado para %s falhou: %s", boleto["nome_cliente"], erro)
            resultados.append((boleto["nome_cliente"], False, str(erro)))
    return resultados


def obter_metricas_boletos():
    """Métricas dos boletos com vencimento no mês atual, para os cards da tela.

    Previsto = tudo que vence no mês; Recebido = o que já está pago;
    Inadimplente = pendente e já vencido; A vencer = pendente dentro do prazo.
    Taxa de inadimplência = inadimplente / previsto."""
    hoje = date.today()
    inicio_mes = hoje.replace(day=1)
    fim_mes = date(hoje.year, hoje.month, calendar.monthrange(hoje.year, hoje.month)[1])

    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT valor, status, data_vencimento FROM cobrancas_boletos "
            "WHERE data_vencimento BETWEEN ? AND ?",
            (inicio_mes.isoformat(), fim_mes.isoformat()),
        ).fetchall()

    previsto = sum(l["valor"] for l in linhas)
    recebido = sum(l["valor"] for l in linhas if l["status"] == "pago")
    inadimplente = sum(
        l["valor"] for l in linhas
        if l["status"] == "pendente" and l["data_vencimento"] < hoje.isoformat()
    )
    a_vencer = previsto - recebido - inadimplente
    qtd_inadimplente = sum(
        1 for l in linhas
        if l["status"] == "pendente" and l["data_vencimento"] < hoje.isoformat()
    )

    return {
        "previsto": previsto,
        "recebido": recebido,
        "inadimplente": inadimplente,
        "a_vencer": a_vencer,
        "taxa_inadimplencia": round(inadimplente / previsto * 100, 1) if previsto else 0.0,
        "qtd": len(linhas),
        "qtd_pagos": sum(1 for l in linhas if l["status"] == "pago"),
        "qtd_inadimplentes": qtd_inadimplente,
        "mes_label": _label_fatura(inicio_mes.isoformat()[:7]),
    }


def obter_historico_recebimentos(meses=6):
    """Total recebido (boletos pagos) por mês de vencimento, nos últimos N meses
    (incluindo o atual). Meses sem recebimento entram com zero para o gráfico
    não pular colunas."""
    inicio_mes_atual = date.today().replace(day=1)
    inicio_janela = _somar_meses(inicio_mes_atual, -(meses - 1))

    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT substr(data_vencimento, 1, 7) AS mes, SUM(valor) AS total "
            "FROM cobrancas_boletos WHERE status = 'pago' AND data_vencimento >= ? "
            "GROUP BY mes",
            (inicio_janela.isoformat(),),
        ).fetchall()
    totais = {l["mes"]: l["total"] for l in linhas}

    historico = []
    for indice in range(meses):
        mes_data = _somar_meses(inicio_janela, indice)
        referencia = mes_data.isoformat()[:7]
        historico.append(
            {
                "mes": referencia,
                "label": f"{MESES_PT[mes_data.month][:3]}/{str(mes_data.year)[2:]}",
                "total": round(totais.get(referencia, 0), 2),
            }
        )
    return historico


def cobrar_boletos_vencidos_em_segundo_plano():
    """Dispara a varredura de cobrança numa thread para não travar a tela.

    Devolve a quantidade de boletos vencidos que serão cobrados (0 = nada a
    fazer — inclusive quando todos os vencidos já foram avisados hoje)."""
    import threading

    qtd = sum(1 for b in listar_regua_cobranca() if b["cenario"] == "vencido")
    if qtd:
        threading.Thread(
            target=verificar_e_cobrar_boletos_vencidos,
            daemon=True,
            name="cobranca-boletos",
        ).start()
        logger.info("Cobrança de %d boleto(s) vencido(s) iniciada em segundo plano.", qtd)
    return qtd


def obter_metricas():
    hoje = date.today()
    hoje_iso = hoje.isoformat()
    inicio_mes_iso = hoje.replace(day=1).isoformat()

    with get_connection() as conexao:
        a_pagar_hoje = conexao.execute(
            "SELECT COUNT(*) AS qtd, COALESCE(SUM(valor), 0) AS total FROM contas "
            "WHERE status = 'pendente' AND vencimento = ?",
            (hoje_iso,),
        ).fetchone()
        pago_mes = conexao.execute(
            "SELECT COUNT(*) AS qtd, COALESCE(SUM(valor), 0) AS total FROM contas "
            "WHERE status = 'pago' AND pago_em >= ?",
            (inicio_mes_iso,),
        ).fetchone()
        vencidos = conexao.execute(
            "SELECT COUNT(*) AS qtd, COALESCE(SUM(valor), 0) AS total FROM contas "
            "WHERE status = 'pendente' AND vencimento < ?",
            (hoje_iso,),
        ).fetchone()

    return {
        "a_pagar_hoje": {
            "valor": a_pagar_hoje["total"],
            "sub": f"{a_pagar_hoje['qtd']} conta(s) com vencimento hoje",
        },
        "pago_mes": {
            "valor": pago_mes["total"],
            "sub": f"{pago_mes['qtd']} conta(s) quitada(s) neste mês",
        },
        "vencidos": {
            "valor": vencidos["total"],
            "sub": f"{vencidos['qtd']} conta(s) em atraso",
        },
    }


def obter_categorias():
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT categoria, COALESCE(SUM(valor), 0) AS total FROM contas "
            "WHERE status != 'pago' GROUP BY categoria ORDER BY total DESC"
        ).fetchall()

    total_geral = sum(linha["total"] for linha in linhas) or 1
    return [
        {
            "nome": CATEGORIAS_LABELS.get(linha["categoria"], linha["categoria"]),
            "valor": linha["total"],
            "percentual": round((linha["total"] / total_geral) * 100),
        }
        for linha in linhas
    ]


def listar_faturas(cartao_id=None):
    """Lista as faturas de cartão de crédito, com o total já lançado no sistema,
    o total lido do PDF e o resultado da conciliação automática de cada conta.

    Com cartao_id, vira uma visão filtrada: cada fatura mostra apenas os
    lançamentos daquele cartão (faturas sem lançamento do cartão saem da lista,
    e os totais do PDF são omitidos porque valem para a fatura inteira)."""
    try:
        cartao_id = int(cartao_id) if cartao_id not in (None, "", "0") else None
    except (TypeError, ValueError):
        cartao_id = None

    filtro_cartao = "AND contas.cartao_id = ? " if cartao_id else ""
    with get_connection() as conexao:
        faturas_linhas = conexao.execute("SELECT * FROM faturas ORDER BY referencia DESC").fetchall()

        resultado = []
        for fatura in faturas_linhas:
            parametros = [fatura["referencia"]] + ([cartao_id] if cartao_id else [])
            contas_linhas = conexao.execute(
                "SELECT contas.nome, contas.valor, contas.categoria, contas.status, contas.conciliacao_pdf, "
                "fornecedores.nome AS fornecedor_nome, cartoes.nome AS cartao_nome FROM contas "
                "LEFT JOIN fornecedores ON fornecedores.id = contas.fornecedor_id "
                "LEFT JOIN cartoes ON cartoes.id = contas.cartao_id "
                "WHERE contas.forma_pagamento = 'cartao' AND contas.fatura_referencia = ? "
                f"{filtro_cartao}ORDER BY contas.nome",
                parametros,
            ).fetchall()

            if cartao_id and not contas_linhas:
                continue  # fatura sem lançamento deste cartão

            valor_lancado = sum(c["valor"] for c in contas_linhas)
            # Na visão filtrada, o total do PDF (fatura inteira) não se aplica.
            valor_informado = None if cartao_id else fatura["valor_informado"]
            encontradas = sum(1 for c in contas_linhas if c["conciliacao_pdf"] == "encontrada")
            nao_encontradas = sum(1 for c in contas_linhas if c["conciliacao_pdf"] == "nao_encontrada")

            resultado.append(
                {
                    "referencia": fatura["referencia"],
                    "label": _label_fatura(fatura["referencia"]),
                    "valor_lancado": valor_lancado,
                    "valor_informado": valor_informado,
                    "total_detectado": fatura["total_detectado"],
                    "diferenca": (valor_informado - valor_lancado) if valor_informado is not None else None,
                    "arquivo_pdf": fatura["arquivo_pdf"],
                    "status": fatura["status"],
                    "qtd": len(contas_linhas),
                    "encontradas": encontradas,
                    "nao_encontradas": nao_encontradas,
                    "contas": [
                        {
                            "nome": c["nome"],
                            "valor": c["valor"],
                            "categoria": CATEGORIAS_LABELS.get(c["categoria"], c["categoria"]),
                            "status": c["status"],
                            "fornecedor": c["fornecedor_nome"],
                            "cartao": c["cartao_nome"],
                            "conciliacao": c["conciliacao_pdf"],
                        }
                        for c in contas_linhas
                    ],
                }
            )
        return resultado


def _conciliar_contas_por_valor(contas_linhas, valores_pdf):
    """Casa cada conta cadastrada com um valor lido do PDF (pelo valor exato).

    Cada valor do PDF só pode "quitar" uma conta: se houver duas contas de
    R$ 100,00 mas o PDF só tiver um 100,00, apenas uma é marcada como encontrada.
    Devolve {id_da_conta: 'encontrada' | 'nao_encontrada'}.
    """
    disponiveis = list(valores_pdf)
    resultado = {}
    for conta in contas_linhas:
        achado = next((v for v in disponiveis if abs(v - conta["valor"]) < 0.005), None)
        if achado is not None:
            disponiveis.remove(achado)
            resultado[conta["id"]] = "encontrada"
        else:
            resultado[conta["id"]] = "nao_encontrada"
    return resultado


def conciliar_fatura(referencia, valor_informado=None, caminho_pdf=None, caminho_absoluto=None):
    """Concilia a fatura do mês com as contas lançadas no sistema.

    Se um PDF foi enviado (caminho_absoluto), lê os valores de dentro dele:
    detecta o total automaticamente e marca conta a conta se o valor dela
    aparece na fatura. O valor digitado à mão vira apenas um complemento para
    quando o PDF não puder ser lido (ex: fatura digitalizada em imagem).
    """
    valor_manual = None
    if valor_informado not in (None, ""):
        try:
            valor_manual = float(valor_informado)
        except (TypeError, ValueError):
            raise ContaInvalida("O valor total informado não é um número válido.")

    valores_pdf, total_pdf = [], None
    if caminho_absoluto:
        try:
            valores_pdf, total_pdf = pdf_fatura.extrair_dados_fatura(caminho_absoluto)
        except pdf_fatura.LeituraFaturaErro as erro:
            raise ContaInvalida(str(erro))

    # Prioridade: valor digitado > total detectado no PDF.
    valor_final = valor_manual if valor_manual is not None else total_pdf
    if valor_final is None and not valores_pdf:
        raise ContaInvalida(
            "Não consegui ler valores no PDF (pode ser uma fatura digitalizada). "
            "Informe o valor total manualmente e envie de novo."
        )

    try:
        with get_connection() as conexao:
            fatura = conexao.execute("SELECT * FROM faturas WHERE referencia = ?", (referencia,)).fetchone()
            if fatura is None:
                raise ContaInvalida("Fatura não encontrada.")

            contas_linhas = conexao.execute(
                "SELECT id, valor FROM contas WHERE forma_pagamento = 'cartao' AND fatura_referencia = ?",
                (referencia,),
            ).fetchall()
            valor_lancado = sum(c["valor"] for c in contas_linhas)

            # Conciliação conta a conta pelo valor lido no PDF.
            if valores_pdf:
                conciliacao = _conciliar_contas_por_valor(contas_linhas, valores_pdf)
                for conta_id, situacao in conciliacao.items():
                    conexao.execute("UPDATE contas SET conciliacao_pdf = ? WHERE id = ?", (situacao, conta_id))
                todas_encontradas = contas_linhas and all(s == "encontrada" for s in conciliacao.values())
            else:
                todas_encontradas = False

            if valor_final is not None:
                novo_status = "conciliada" if abs(valor_final - valor_lancado) < 0.01 else "divergente"
            else:
                novo_status = "conciliada" if todas_encontradas else "divergente"

            arquivo_final = caminho_pdf or fatura["arquivo_pdf"]
            conexao.execute(
                "UPDATE faturas SET valor_informado = ?, total_detectado = ?, arquivo_pdf = ?, status = ?, "
                "conciliada_em = ? WHERE referencia = ?",
                (
                    valor_final,
                    total_pdf,
                    arquivo_final,
                    novo_status,
                    datetime.now().isoformat(timespec="seconds"),
                    referencia,
                ),
            )
        logger.info(
            "Fatura %s conciliada: status=%s (lançado R$ %s, total considerado R$ %s, %d valor(es) lidos do PDF).",
            referencia,
            novo_status,
            formatar_moeda(valor_lancado),
            formatar_moeda(valor_final) if valor_final is not None else "--",
            len(valores_pdf),
        )
        return novo_status
    except sqlite3.Error:
        logger.exception("Falha ao conciliar a fatura %s", referencia)
        raise ContaInvalida("Não foi possível salvar a conciliação da fatura.")


def obter_resumo_periodo(data_inicio, data_fim):
    """Resumo estruturado das contas com vencimento dentro do período: lista + totais."""
    hoje = date.today()
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT nome, valor, status, categoria, vencimento FROM contas "
            "WHERE vencimento BETWEEN ? AND ? ORDER BY vencimento ASC, nome ASC",
            (data_inicio.isoformat(), data_fim.isoformat()),
        ).fetchall()

    contas = [
        {
            "nome": linha["nome"],
            "valor": linha["valor"],
            "categoria": CATEGORIAS_LABELS.get(linha["categoria"], linha["categoria"]),
            "status": _status_exibicao(linha["status"], linha["vencimento"], hoje),
            "vencimento": _formatar_data_br(linha["vencimento"]),
        }
        for linha in linhas
    ]

    total_pago = sum(c["valor"] for c in contas if c["status"] == "pago")
    total = sum(c["valor"] for c in contas)
    return {
        "contas": contas,
        "qtd": len(contas),
        "total": total,
        "total_pago": total_pago,
        "total_pendente": total - total_pago,
    }


def obter_maiores_ofensores(data_inicio, data_fim, limite=5):
    """Top 'ofensores' do período: quem mais consumiu recursos.

    Agrupa cada conta pelo fornecedor quando há um vinculado; sem fornecedor,
    agrupa pela categoria. Devolve os maiores com o % que cada um representa
    do total gasto no período (o % é sobre o total geral, não só sobre o top)."""
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT COALESCE(fornecedores.nome, contas.categoria) AS ofensor, "
            "CASE WHEN fornecedores.nome IS NOT NULL THEN 'fornecedor' ELSE 'categoria' END AS tipo, "
            "SUM(contas.valor) AS total, COUNT(*) AS qtd "
            "FROM contas "
            "LEFT JOIN fornecedores ON fornecedores.id = contas.fornecedor_id "
            "WHERE contas.vencimento BETWEEN ? AND ? "
            "GROUP BY ofensor, tipo ORDER BY total DESC",
            (data_inicio.isoformat(), data_fim.isoformat()),
        ).fetchall()

    total_geral = sum(linha["total"] for linha in linhas)
    if not total_geral:
        return []

    return [
        {
            "nome": CATEGORIAS_LABELS.get(linha["ofensor"], linha["ofensor"]) if linha["tipo"] == "categoria" else linha["ofensor"],
            "tipo": linha["tipo"],
            "total": linha["total"],
            "qtd": linha["qtd"],
            "percentual": round(linha["total"] / total_geral * 100, 1),
        }
        for linha in linhas[:limite]
    ]


def gerar_relatorio_texto(data_inicio, data_fim=None):
    """Texto do relatório para o WhatsApp.

    Um dia só mantém o formato clássico; um período agrupa as contas por dia
    de vencimento, com subtotal em cada dia.
    """
    if data_fim is None or data_fim == data_inicio:
        data_fim = data_inicio
        titulo = f"📊 *Relatório de Contas do Dia ({_formatar_data_br(data_inicio.isoformat())})*"
        sem_contas = "Nenhuma conta com vencimento nesta data."
    else:
        titulo = (
            f"📊 *Relatório de Contas ({_formatar_data_br(data_inicio.isoformat())} "
            f"a {_formatar_data_br(data_fim.isoformat())})*"
        )
        sem_contas = "Nenhuma conta com vencimento neste período."

    resumo = obter_resumo_periodo(data_inicio, data_fim)

    if not resumo["contas"]:
        corpo = sem_contas
        rodape = f"💰 *Valor Total: R$ {formatar_moeda(0)}*"
    elif data_fim == data_inicio:
        corpo = "\n".join(
            f"{'✅' if conta['status'] == 'pago' else '⏳'} {conta['nome']}: R$ {formatar_moeda(conta['valor'])}"
            for conta in resumo["contas"]
        )
        rodape = (
            f"💰 *Valor Total: R$ {formatar_moeda(resumo['total'])}*\n"
            f"✅ Pago: R$ {formatar_moeda(resumo['total_pago'])} · "
            f"⏳ Pendente: R$ {formatar_moeda(resumo['total_pendente'])}"
        )
    else:
        blocos = []
        dia_atual, linhas_dia, subtotal = None, [], 0
        for conta in resumo["contas"]:
            if conta["vencimento"] != dia_atual:
                if linhas_dia:
                    blocos.append("\n".join(linhas_dia + [f"   Subtotal: R$ {formatar_moeda(subtotal)}"]))
                dia_atual, linhas_dia, subtotal = conta["vencimento"], [f"📅 *{conta['vencimento']}*"], 0
            marca = "✅" if conta["status"] == "pago" else "⏳"
            linhas_dia.append(f"{marca} {conta['nome']}: R$ {formatar_moeda(conta['valor'])}")
            subtotal += conta["valor"]
        if linhas_dia:
            blocos.append("\n".join(linhas_dia + [f"   Subtotal: R$ {formatar_moeda(subtotal)}"]))
        corpo = "\n\n".join(blocos)
        rodape = (
            f"💰 *Valor Total: R$ {formatar_moeda(resumo['total'])}* ({resumo['qtd']} conta(s))\n"
            f"✅ Pago: R$ {formatar_moeda(resumo['total_pago'])} · "
            f"⏳ Pendente: R$ {formatar_moeda(resumo['total_pendente'])}"
        )

    return (
        f"{titulo}\n"
        f"{corpo}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{rodape}"
    )


# ==========================================================================
# Auditoria com IA (botão "Auditar Finanças com IA" na Central de Relatórios)
# ==========================================================================

PROMPT_AUDITOR_IA = """Você é um auditor financeiro experiente analisando as contas a pagar de uma clínica odontopediátrica. Você receberá as contas do mês atual e os totais por categoria dos meses anteriores.

Produza um diagnóstico em português do Brasil, direto e prático, com estas seções:

🔍 VISÃO GERAL — 2 ou 3 frases sobre a saúde financeira do mês.

📈 CATEGORIAS FORA DA CURVA — compare o gasto de cada categoria do mês atual com a média dos meses anteriores; aponte apenas as que subiram ou caíram de forma relevante, citando os valores em R$ e o percentual. Se o histórico for curto demais para comparar, diga isso.

👀 SUSPEITAS DE DUPLICIDADE — contas com mesmo nome e valor que podem ser lançamentos duplicados. Ignore parcelas "(1/N)" e recorrências "(Mês/Ano)": são lançamentos legítimos gerados pelo sistema.

💡 ONDE ECONOMIZAR — 2 a 4 dicas concretas baseadas nos dados fornecidos, não genéricas.

Regras: use apenas os dados fornecidos, sem inventar números; escreva em texto simples (sem markdown e sem tabelas), com os títulos das seções exatamente como acima e hífens para listas; valores sempre no formato R$ 1.234,56. Se não houver nada relevante em uma seção, diga isso em uma linha só."""


def _montar_contexto_auditoria():
    """Monta o texto com os dados financeiros que a IA vai analisar:
    todas as contas do mês atual + totais por categoria dos últimos 6 meses."""
    hoje = date.today()
    inicio_mes = hoje.replace(day=1)
    ultimo_dia = date(hoje.year, hoje.month, calendar.monthrange(hoje.year, hoje.month)[1])
    inicio_historico = _somar_meses(inicio_mes, -6)

    with get_connection() as conexao:
        contas_mes = conexao.execute(
            "SELECT contas.nome, contas.valor, contas.vencimento, contas.categoria, contas.status, "
            "contas.forma_pagamento, fornecedores.nome AS fornecedor_nome FROM contas "
            "LEFT JOIN fornecedores ON fornecedores.id = contas.fornecedor_id "
            "WHERE contas.vencimento BETWEEN ? AND ? "
            "ORDER BY contas.vencimento ASC, contas.nome ASC",
            (inicio_mes.isoformat(), ultimo_dia.isoformat()),
        ).fetchall()

        historico = conexao.execute(
            "SELECT substr(vencimento, 1, 7) AS mes, categoria, SUM(valor) AS total, COUNT(*) AS qtd "
            "FROM contas WHERE vencimento >= ? AND vencimento < ? "
            "GROUP BY mes, categoria ORDER BY mes ASC, total DESC",
            (inicio_historico.isoformat(), inicio_mes.isoformat()),
        ).fetchall()

    if not contas_mes:
        raise ContaInvalida(
            "Nenhuma conta com vencimento neste mês para auditar. "
            "Cadastre as contas do mês antes de rodar a auditoria."
        )

    linhas = [f"Hoje é {_formatar_data_br(hoje.isoformat())}.", ""]
    linhas.append(f"CONTAS DO MÊS ATUAL ({_label_fatura(inicio_mes.isoformat()[:7])}):")
    for c in contas_mes:
        fornecedor = f" | fornecedor: {c['fornecedor_nome']}" if c["fornecedor_nome"] else ""
        forma = FORMAS_PAGAMENTO_LABELS.get(c["forma_pagamento"], c["forma_pagamento"])
        linhas.append(
            f"- {_formatar_data_br(c['vencimento'])} | {c['nome']} | "
            f"{CATEGORIAS_LABELS.get(c['categoria'], c['categoria'])} | R$ {formatar_moeda(c['valor'])} | "
            f"{c['status']} | {forma}{fornecedor}"
        )

    linhas.append("")
    if historico:
        linhas.append("TOTAIS POR CATEGORIA NOS MESES ANTERIORES (para comparação com a média histórica):")
        for h in historico:
            linhas.append(
                f"- {_label_fatura(h['mes'])} | {CATEGORIAS_LABELS.get(h['categoria'], h['categoria'])} | "
                f"R$ {formatar_moeda(h['total'])} ({h['qtd']} conta(s))"
            )
    else:
        linhas.append("SEM HISTÓRICO: não há contas registradas nos meses anteriores.")

    return "\n".join(linhas)


def auditar_financas_ia():
    """Envia o resumo do mês para o Claude e devolve o diagnóstico em texto."""
    import anthropic  # import local: o restante do sistema funciona sem o SDK

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ContaInvalida(
            "A auditoria com IA precisa de uma chave da API do Claude. Crie a sua em "
            "platform.claude.com, defina a variável de ambiente ANTHROPIC_API_KEY e "
            "reinicie o sistema."
        )

    contexto = _montar_contexto_auditoria()

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=PROMPT_AUDITOR_IA,
            messages=[{"role": "user", "content": contexto}],
        )
    except anthropic.AuthenticationError:
        raise ContaInvalida(
            "A chave da API do Claude foi rejeitada. Confira o valor da variável "
            "ANTHROPIC_API_KEY em platform.claude.com."
        )
    except anthropic.RateLimitError:
        raise ContaInvalida("Muitas auditorias em sequência. Aguarde um minuto e tente de novo.")
    except anthropic.APIConnectionError:
        raise ContaInvalida("Sem conexão com a API do Claude. Verifique a sua internet e tente de novo.")
    except anthropic.APIStatusError as erro:
        logger.exception("Erro da API do Claude na auditoria")
        raise ContaInvalida(f"A API do Claude retornou um erro ({erro.status_code}). Tente de novo em instantes.")

    if response.stop_reason == "refusal":
        raise ContaInvalida("A IA não pôde analisar estes dados. Tente novamente.")

    texto = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not texto:
        raise ContaInvalida("A IA não retornou nenhum diagnóstico. Tente de novo.")

    logger.info(
        "Auditoria com IA concluída (%d tokens de entrada, %d de saída).",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return texto


# ==========================================================================
# Oráculo Financeiro (Simulador de Cenários): projeta o fluxo de caixa dos
# próximos meses e mede o impacto de um gasto novo ANTES de assumir o
# compromisso — sem gravar nada no banco.
# ==========================================================================

MESES_PROJECAO_ORACULO = 6
DURACAO_MAX_SIMULACAO = 60


def projetar_fluxo_caixa(meses=MESES_PROJECAO_ORACULO):
    """Fluxo de caixa previsto por mês (atual + próximos), com o que já está no
    banco: receitas = boletos a receber (pagos e pendentes) por mês de
    vencimento; despesas = contas a pagar por mês de vencimento. Meses sem
    lançamento entram zerados para o gráfico não pular colunas."""
    inicio = date.today().replace(day=1)
    fim = _somar_meses(inicio, meses)

    with get_connection() as conexao:
        contas = conexao.execute(
            "SELECT substr(vencimento, 1, 7) AS ref, SUM(valor) AS total FROM contas "
            "WHERE vencimento >= ? AND vencimento < ? GROUP BY ref",
            (inicio.isoformat(), fim.isoformat()),
        ).fetchall()
        boletos = conexao.execute(
            "SELECT substr(data_vencimento, 1, 7) AS ref, SUM(valor) AS total "
            "FROM cobrancas_boletos "
            "WHERE data_vencimento >= ? AND data_vencimento < ? GROUP BY ref",
            (inicio.isoformat(), fim.isoformat()),
        ).fetchall()

    despesas_mes = {linha["ref"]: linha["total"] for linha in contas}
    receitas_mes = {linha["ref"]: linha["total"] for linha in boletos}

    projecao = []
    for indice in range(meses):
        ref = _somar_meses(inicio, indice).isoformat()[:7]
        receitas = receitas_mes.get(ref, 0.0)
        despesas = despesas_mes.get(ref, 0.0)
        projecao.append(
            {
                "referencia": ref,
                "label": _label_fatura(ref),
                "receitas": receitas,
                "despesas": despesas,
                "saldo": round(receitas - despesas, 2),
            }
        )
    return projecao


def simular_cenario_oraculo(valor_mensal, categoria, duracao_meses):
    """Injeta o gasto simulado no fluxo previsto e devolve os dados do gráfico
    comparativo (saldo real × saldo com o cenário) mais o veredito do caixa.

    O gasto entra a partir do mês atual e dura 'duracao_meses' (o que passar da
    janela de projeção conta no custo total, mas não aparece no gráfico). O
    veredito olha o pior mês simulado: nenhum mês negativo = 'seguro'; algum mês
    negativo = 'vermelho', avisando se o caixa já estava negativo antes."""
    try:
        valor = float(valor_mensal)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe um valor mensal numérico para a simulação.")
    if valor <= 0:
        raise ContaInvalida("O valor mensal simulado deve ser maior que zero.")

    categoria = (categoria or "").strip()
    if not categoria:
        raise ContaInvalida("Escolha a categoria do gasto simulado.")

    try:
        duracao = int(duracao_meses)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe a duração da despesa em meses (número inteiro).")
    if not 1 <= duracao <= DURACAO_MAX_SIMULACAO:
        raise ContaInvalida(f"A duração deve ficar entre 1 e {DURACAO_MAX_SIMULACAO} meses.")

    projecao = projetar_fluxo_caixa()
    for indice, mes in enumerate(projecao):
        gasto_extra = valor if indice < duracao else 0.0
        mes["saldo_simulado"] = round(mes["saldo"] - gasto_extra, 2)

    pior = min(projecao, key=lambda mes: mes["saldo_simulado"])
    primeiro_vermelho = next((mes for mes in projecao if mes["saldo_simulado"] < 0), None)

    if primeiro_vermelho is None:
        nivel = "seguro"
        frase = (
            f"Sinal verde! Mesmo pagando R$ {formatar_moeda(valor)} por mês, o caixa "
            f"fecha no azul em todos os {len(projecao)} meses projetados. O mês mais "
            f"apertado seria {pior['label']}, com R$ {formatar_moeda(pior['saldo_simulado'])} de folga."
        )
    elif primeiro_vermelho["saldo"] < 0:
        nivel = "vermelho"
        frase = (
            f"Atenção: o caixa já fica negativo em {primeiro_vermelho['label']} mesmo sem o "
            f"gasto novo. Com a simulação, o pior mês ({pior['label']}) fecha em "
            f"R$ {formatar_moeda(pior['saldo_simulado'])}. Melhor resolver o caixa antes de investir."
        )
    else:
        nivel = "vermelho"
        frase = (
            f"Sinal vermelho: este gasto joga o caixa para o negativo em "
            f"{primeiro_vermelho['label']} (saldo projetado de "
            f"R$ {formatar_moeda(primeiro_vermelho['saldo_simulado'])}). Reduza o valor ou a duração."
        )

    logger.info(
        "Oráculo: simulação de R$ %s/mês por %d mês(es) em '%s' → veredito '%s'.",
        formatar_moeda(valor), duracao, categoria, nivel,
    )
    return {
        "parametros": {
            "valor_mensal": valor,
            "categoria": categoria,
            "duracao_meses": duracao,
            "custo_total": round(valor * duracao, 2),
        },
        "projecao": projecao,
        "resumo": {
            "nivel": nivel,
            "frase": frase,
            "pior_mes_label": pior["label"],
            "pior_mes_saldo": pior["saldo_simulado"],
        },
    }


# ==========================================================================
# Radar de Gargalos Financeiros: compara o ritmo de gastos de cada categoria
# no começo do mês (primeiros 10 e 20 dias) com a média histórica do mesmo
# trecho dos meses anteriores, para pegar vazamento de caixa antes de o mês
# fechar.
# ==========================================================================

DIAS_CHECKPOINT_RADAR = (10, 20)
MESES_HISTORICO_RADAR = 6
# Uma categoria só vira alerta se estourar a média histórica em 30% E o excesso
# passar de R$ 100 — os dois juntos evitam alarme falso em categorias pequenas.
LIMITE_ACELERACAO_RADAR = 30.0
EXCESSO_MINIMO_RADAR = 100.0


def detectar_gargalos_financeiros():
    """Alertas de categoria gastando acelerado no mês atual.

    Usa o último checkpoint já alcançado (dia 10 ou dia 20). Para cada
    categoria, soma as contas com vencimento nos primeiros N dias do mês atual
    e compara com a média do MESMO trecho (dias 1..N) dos últimos 6 meses —
    a comparação proporcional evita comparar 10 dias de agora com 30 de antes.
    Categorias com menos de 2 meses de histórico ficam de fora (padrão fraco).
    Devolve os alertas ordenados do estouro maior para o menor."""
    hoje = date.today()
    checkpoints = [dia for dia in DIAS_CHECKPOINT_RADAR if hoje.day >= dia]
    if not checkpoints:
        return []  # mês recém-começado: ainda não há ritmo para medir
    checkpoint = max(checkpoints)

    inicio_mes = hoje.replace(day=1)
    inicio_historico = _somar_meses(inicio_mes, -MESES_HISTORICO_RADAR)
    ref_atual = inicio_mes.isoformat()[:7]

    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT categoria, vencimento, valor FROM contas "
            "WHERE vencimento >= ? AND vencimento < ?",
            (inicio_historico.isoformat(), _somar_meses(inicio_mes, 1).isoformat()),
        ).fetchall()

    gasto_atual = {}  # categoria -> total nos primeiros N dias do mês atual
    historico = {}    # categoria -> {mês: total nos primeiros N dias daquele mês}
    for linha in linhas:
        if int(linha["vencimento"][8:10]) > checkpoint:
            continue
        referencia = linha["vencimento"][:7]
        if referencia == ref_atual:
            gasto_atual[linha["categoria"]] = gasto_atual.get(linha["categoria"], 0.0) + linha["valor"]
        else:
            meses = historico.setdefault(linha["categoria"], {})
            meses[referencia] = meses.get(referencia, 0.0) + linha["valor"]

    alertas = []
    for categoria, gasto in gasto_atual.items():
        meses = historico.get(categoria, {})
        if len(meses) < 2:
            continue
        media = sum(meses.values()) / len(meses)
        if media <= 0:
            continue
        percentual_acima = (gasto / media - 1) * 100
        if percentual_acima >= LIMITE_ACELERACAO_RADAR and (gasto - media) >= EXCESSO_MINIMO_RADAR:
            alertas.append(
                {
                    "categoria": CATEGORIAS_LABELS.get(categoria, categoria),
                    "checkpoint": checkpoint,
                    "gasto_atual": round(gasto, 2),
                    "media_historica": round(media, 2),
                    "excesso": round(gasto - media, 2),
                    "percentual_acima": round(percentual_acima),
                    "meses_no_historico": len(meses),
                }
            )

    alertas.sort(key=lambda alerta: alerta["percentual_acima"], reverse=True)
    if alertas:
        logger.info(
            "Radar de Gargalos: %d categoria(s) acelerada(s) nos primeiros %d dias do mês.",
            len(alertas), checkpoint,
        )
    return alertas


# ==========================================================================
# Prontuário Digital do Paciente: pacientes, tabela de preços (procedimentos),
# colaboradores, orçamentos e checkout financeiro. O checkout de um orçamento
# aprovado gera a cobrança em cobrancas_boletos (elo orcamento_id), então a
# receita entra automaticamente na Meta, no Termômetro de Risco e no Oráculo.
# ==========================================================================

FORMAS_CHECKOUT = {"avista": "À vista", "boleto": "Boleto"}
STATUS_ORCAMENTO_LABELS = {"pendente": "Pendente", "aprovado": "Aprovado", "faturado": "Faturado"}


def _normalizar_cpf(cpf):
    """CPF é opcional; quando informado precisa ter 11 dígitos e é guardado
    formatado (000.000.000-00) para exibição e busca."""
    digitos = re.sub(r"\D", "", cpf or "")
    if not digitos:
        return None
    if len(digitos) != 11:
        raise ContaInvalida("CPF inválido: precisa ter 11 dígitos.")
    return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"


def _validar_paciente(nome, telefone, cpf, data_nascimento):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome do paciente.")
    telefone = _normalizar_telefone_cliente(telefone)
    cpf = _normalizar_cpf(cpf)
    nascimento = None
    if (data_nascimento or "").strip():
        try:
            nascimento = datetime.strptime(data_nascimento, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ContaInvalida("Data de nascimento inválida.")
        if nascimento > date.today():
            raise ContaInvalida("A data de nascimento não pode estar no futuro.")
    return nome, telefone, cpf, nascimento.isoformat() if nascimento else None


def listar_pacientes():
    """Lista para a tela de Pacientes, com a contagem de orçamentos de cada um."""
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT p.id, p.nome, p.telefone, p.cpf, p.data_nascimento, "
            "COUNT(o.id) AS qtd_orcamentos "
            "FROM pacientes p LEFT JOIN orcamentos o ON o.paciente_id = p.id "
            "GROUP BY p.id ORDER BY p.nome COLLATE NOCASE ASC"
        ).fetchall()
    return [
        {
            "id": l["id"],
            "nome": l["nome"],
            "telefone": l["telefone"],
            "cpf": l["cpf"] or "—",
            "nascimento": _formatar_data_br(l["data_nascimento"]) if l["data_nascimento"] else "—",
            "qtd_orcamentos": l["qtd_orcamentos"],
        }
        for l in linhas
    ]


def pesquisar_pacientes(termo, limite=8):
    """Barra de Pesquisa Global: busca por nome OU CPF (LIKE), devolvendo o
    essencial para o dropdown (id, nome, cpf). Termo curto devolve vazio."""
    termo = (termo or "").strip()
    if len(termo) < 2:
        return []
    # % e _ do usuário viram literais para o LIKE não virar curinga acidental.
    padrao = "%" + re.sub(r"([%_\\])", r"\\\1", termo) + "%"
    # CPF pode ser digitado com ou sem pontuação; compara só os dígitos.
    padrao_cpf = "%" + re.sub(r"\D", "", termo) + "%" if re.sub(r"\D", "", termo) else None
    try:
        with get_connection() as conexao:
            linhas = conexao.execute(
                "SELECT id, nome, cpf, foto FROM pacientes "
                "WHERE nome LIKE ? ESCAPE '\\' "
                "   OR (cpf IS NOT NULL AND replace(replace(cpf, '.', ''), '-', '') LIKE ?) "
                "ORDER BY nome COLLATE NOCASE ASC LIMIT ?",
                (padrao, padrao_cpf or padrao, limite),
            ).fetchall()
        return [
            {"id": l["id"], "nome": l["nome"], "cpf": l["cpf"] or "", "tem_foto": bool(l["foto"])}
            for l in linhas
        ]
    except sqlite3.Error:
        logger.exception("Falha na pesquisa global de pacientes ('%s')", termo)
        return []


def obter_paciente(paciente_id):
    with get_connection() as conexao:
        linha = conexao.execute(
            "SELECT id, nome, telefone, cpf, data_nascimento, foto, criado_em "
            "FROM pacientes WHERE id = ?",
            (paciente_id,),
        ).fetchone()
    if not linha:
        raise ContaInvalida("Paciente não encontrado.")
    return {
        "id": linha["id"],
        "nome": linha["nome"],
        "telefone": linha["telefone"],
        "cpf": linha["cpf"] or "",
        "data_nascimento": linha["data_nascimento"] or "",
        "nascimento_br": _formatar_data_br(linha["data_nascimento"]) if linha["data_nascimento"] else "—",
        "foto": linha["foto"] or "",
        "criado_em": _formatar_data_br(linha["criado_em"][:10]) if linha["criado_em"] else "—",
    }


def salvar_foto_paciente(paciente_id, nome_arquivo):
    """Grava o nome do arquivo da foto (salvo em uploads/fotos_pacientes/)."""
    obter_paciente(paciente_id)  # garante que o paciente existe
    try:
        with get_connection() as conexao:
            conexao.execute(
                "UPDATE pacientes SET foto = ? WHERE id = ?", (nome_arquivo, paciente_id)
            )
        logger.info("Foto do paciente %s atualizada (%s).", paciente_id, nome_arquivo)
    except sqlite3.Error:
        logger.exception("Falha ao salvar a foto do paciente %s", paciente_id)
        raise ContaInvalida("Não foi possível salvar a foto do paciente.")


def obter_foto_paciente(paciente_id):
    with get_connection() as conexao:
        linha = conexao.execute(
            "SELECT foto FROM pacientes WHERE id = ?", (paciente_id,)
        ).fetchone()
    return linha["foto"] if linha and linha["foto"] else None


def criar_paciente(nome, telefone, cpf, data_nascimento):
    nome, telefone, cpf, nascimento = _validar_paciente(nome, telefone, cpf, data_nascimento)
    try:
        with get_connection() as conexao:
            if cpf and conexao.execute("SELECT 1 FROM pacientes WHERE cpf = ?", (cpf,)).fetchone():
                raise ContaInvalida("Já existe um paciente com este CPF.")
            cursor = conexao.execute(
                "INSERT INTO pacientes (nome, telefone, cpf, data_nascimento) VALUES (?, ?, ?, ?)",
                (nome, telefone, cpf, nascimento),
            )
        logger.info("Paciente '%s' cadastrado (id %s).", nome, cursor.lastrowid)
        return cursor.lastrowid
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o paciente '%s'", nome)
        raise ContaInvalida("Não foi possível salvar o paciente no banco de dados.")


def editar_paciente(paciente_id, nome, telefone, cpf, data_nascimento):
    obter_paciente(paciente_id)  # garante que existe (ou levanta erro amigável)
    nome, telefone, cpf, nascimento = _validar_paciente(nome, telefone, cpf, data_nascimento)
    try:
        with get_connection() as conexao:
            if cpf and conexao.execute(
                "SELECT 1 FROM pacientes WHERE cpf = ? AND id != ?", (cpf, paciente_id)
            ).fetchone():
                raise ContaInvalida("Já existe outro paciente com este CPF.")
            conexao.execute(
                "UPDATE pacientes SET nome = ?, telefone = ?, cpf = ?, data_nascimento = ? WHERE id = ?",
                (nome, telefone, cpf, nascimento, paciente_id),
            )
        logger.info("Paciente %s atualizado.", paciente_id)
    except sqlite3.Error:
        logger.exception("Falha ao atualizar o paciente %s", paciente_id)
        raise ContaInvalida("Não foi possível salvar as alterações do paciente.")


def excluir_paciente(paciente_id):
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM orcamentos WHERE paciente_id = ?", (paciente_id,)
            ).fetchone():
                raise ContaInvalida(
                    "Este paciente tem orçamentos no prontuário. Exclua os orçamentos "
                    "não faturados antes; prontuários com checkout feito ficam guardados."
                )
            conexao.execute("DELETE FROM pacientes WHERE id = ?", (paciente_id,))
        logger.info("Paciente %s excluído.", paciente_id)
    except sqlite3.Error:
        logger.exception("Falha ao excluir o paciente %s", paciente_id)
        raise ContaInvalida("Não foi possível excluir o paciente.")


# ---- Tabela de preços (procedimentos) e colaboradores ----

def listar_procedimentos():
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT id, nome_procedimento, valor_base FROM procedimentos_tabela "
            "ORDER BY nome_procedimento COLLATE NOCASE ASC"
        ).fetchall()
    return [{"id": l["id"], "nome": l["nome_procedimento"], "valor": l["valor_base"]} for l in linhas]


def adicionar_procedimento(nome, valor_base):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome do procedimento.")
    try:
        valor = float(str(valor_base).replace(".", "").replace(",", ".")) \
            if isinstance(valor_base, str) and "," in str(valor_base) else float(valor_base)
    except (TypeError, ValueError):
        raise ContaInvalida("Informe um valor numérico válido para o procedimento.")
    if valor <= 0:
        raise ContaInvalida("O valor do procedimento deve ser maior que zero.")
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM procedimentos_tabela WHERE nome_procedimento = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe um procedimento com este nome.")
            conexao.execute(
                "INSERT INTO procedimentos_tabela (nome_procedimento, valor_base) VALUES (?, ?)",
                (nome, valor),
            )
        logger.info("Procedimento '%s' adicionado à tabela de preços (R$ %s).", nome, formatar_moeda(valor))
    except sqlite3.IntegrityError:
        raise ContaInvalida("Já existe um procedimento com este nome.")
    except sqlite3.Error:
        logger.exception("Falha ao adicionar o procedimento '%s'", nome)
        raise ContaInvalida("Não foi possível salvar o procedimento.")


def remover_procedimento(procedimento_id):
    try:
        with get_connection() as conexao:
            conexao.execute("DELETE FROM procedimentos_tabela WHERE id = ?", (procedimento_id,))
        logger.info("Procedimento %s removido da tabela de preços.", procedimento_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o procedimento %s", procedimento_id)
        raise ContaInvalida("Não foi possível remover o procedimento.")


def listar_colaboradores():
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT id, nome FROM colaboradores ORDER BY nome COLLATE NOCASE ASC"
        ).fetchall()
    return [{"id": l["id"], "nome": l["nome"]} for l in linhas]


def adicionar_colaborador(nome):
    nome = (nome or "").strip()
    if not nome:
        raise ContaInvalida("Informe o nome do colaborador.")
    try:
        with get_connection() as conexao:
            if conexao.execute(
                "SELECT 1 FROM colaboradores WHERE nome = ? COLLATE NOCASE", (nome,)
            ).fetchone():
                raise ContaInvalida("Já existe um colaborador com este nome.")
            conexao.execute("INSERT INTO colaboradores (nome) VALUES (?)", (nome,))
        logger.info("Colaborador '%s' cadastrado.", nome)
    except sqlite3.IntegrityError:
        raise ContaInvalida("Já existe um colaborador com este nome.")
    except sqlite3.Error:
        logger.exception("Falha ao cadastrar o colaborador '%s'", nome)
        raise ContaInvalida("Não foi possível salvar o colaborador.")


def remover_colaborador(colaborador_id):
    try:
        with get_connection() as conexao:
            conexao.execute("DELETE FROM colaboradores WHERE id = ?", (colaborador_id,))
        logger.info("Colaborador %s removido.", colaborador_id)
    except sqlite3.Error:
        logger.exception("Falha ao remover o colaborador %s", colaborador_id)
        raise ContaInvalida("Não foi possível remover o colaborador.")


# ---- Orçamentos do prontuário ----

def listar_orcamentos_paciente(paciente_id):
    """Orçamentos do paciente com nomes dos colaboradores e, quando faturado,
    a situação da cobrança gerada pelo checkout."""
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT o.id, o.descricao_itens, o.valor_total, o.status, o.criado_em, "
            "o.checkout_forma, o.checkout_em, "
            "c1.nome AS colaborador_nome, c2.nome AS checkout_colaborador_nome, "
            "cb.status AS cobranca_status, cb.data_vencimento AS cobranca_vencimento "
            "FROM orcamentos o "
            "LEFT JOIN colaboradores c1 ON c1.id = o.colaborador_id "
            "LEFT JOIN colaboradores c2 ON c2.id = o.checkout_colaborador_id "
            "LEFT JOIN cobrancas_boletos cb ON cb.orcamento_id = o.id "
            "WHERE o.paciente_id = ? ORDER BY o.id DESC",
            (paciente_id,),
        ).fetchall()
    return [
        {
            "id": l["id"],
            "itens": l["descricao_itens"],
            "valor_total": l["valor_total"],
            "status": l["status"],
            "status_label": STATUS_ORCAMENTO_LABELS.get(l["status"], l["status"]),
            "data": _formatar_data_br(l["criado_em"][:10]) if l["criado_em"] else "—",
            "colaborador": l["colaborador_nome"] or "—",
            "checkout_forma": FORMAS_CHECKOUT.get(l["checkout_forma"], l["checkout_forma"] or ""),
            "checkout_em": _formatar_data_br(l["checkout_em"]) if l["checkout_em"] else "",
            "checkout_colaborador": l["checkout_colaborador_nome"] or "",
            "cobranca_status": l["cobranca_status"] or "",
            "cobranca_vencimento": _formatar_data_br(l["cobranca_vencimento"]) if l["cobranca_vencimento"] else "",
        }
        for l in linhas
    ]


def criar_orcamento(paciente_id, procedimento_ids, colaborador_id):
    """Monta o orçamento a partir dos procedimentos escolhidos na tabela de
    preços: a descrição guarda nome e valor de cada item e o total é a soma."""
    obter_paciente(paciente_id)

    ids = [int(i) for i in (procedimento_ids or []) if str(i).strip().isdigit()]
    if not ids:
        raise ContaInvalida("Escolha pelo menos um procedimento para o orçamento.")

    colaborador = None
    if (str(colaborador_id) or "").strip().isdigit():
        colaborador = int(colaborador_id)

    try:
        with get_connection() as conexao:
            marcadores = ",".join("?" * len(ids))
            linhas = conexao.execute(
                f"SELECT id, nome_procedimento, valor_base FROM procedimentos_tabela WHERE id IN ({marcadores})",
                ids,
            ).fetchall()
            if len(linhas) != len(set(ids)):
                raise ContaInvalida("Um dos procedimentos escolhidos não existe mais na tabela de preços.")
            if colaborador and not conexao.execute(
                "SELECT 1 FROM colaboradores WHERE id = ?", (colaborador,)
            ).fetchone():
                raise ContaInvalida("Colaborador não encontrado.")

            descricao = "; ".join(
                f"{l['nome_procedimento']} (R$ {formatar_moeda(l['valor_base'])})" for l in linhas
            )
            total = round(sum(l["valor_base"] for l in linhas), 2)
            conexao.execute(
                "INSERT INTO orcamentos (paciente_id, colaborador_id, descricao_itens, valor_total, status) "
                "VALUES (?, ?, ?, ?, 'pendente')",
                (paciente_id, colaborador, descricao, total),
            )
        logger.info(
            "Orçamento criado para o paciente %s: %d item(ns), total R$ %s.",
            paciente_id, len(linhas), formatar_moeda(total),
        )
    except sqlite3.Error:
        logger.exception("Falha ao criar orçamento para o paciente %s", paciente_id)
        raise ContaInvalida("Não foi possível salvar o orçamento.")


def aprovar_orcamento(orcamento_id):
    try:
        with get_connection() as conexao:
            linha = conexao.execute(
                "SELECT status FROM orcamentos WHERE id = ?", (orcamento_id,)
            ).fetchone()
            if not linha:
                raise ContaInvalida("Orçamento não encontrado.")
            if linha["status"] != "pendente":
                raise ContaInvalida("Só orçamentos pendentes podem ser aprovados.")
            conexao.execute("UPDATE orcamentos SET status = 'aprovado' WHERE id = ?", (orcamento_id,))
        logger.info("Orçamento %s aprovado.", orcamento_id)
    except sqlite3.Error:
        logger.exception("Falha ao aprovar o orçamento %s", orcamento_id)
        raise ContaInvalida("Não foi possível aprovar o orçamento.")


def excluir_orcamento(orcamento_id):
    try:
        with get_connection() as conexao:
            linha = conexao.execute(
                "SELECT status FROM orcamentos WHERE id = ?", (orcamento_id,)
            ).fetchone()
            if not linha:
                raise ContaInvalida("Orçamento não encontrado.")
            if linha["status"] == "faturado":
                raise ContaInvalida(
                    "Orçamento faturado não pode ser excluído — a cobrança dele já está no financeiro."
                )
            conexao.execute("DELETE FROM orcamentos WHERE id = ?", (orcamento_id,))
        logger.info("Orçamento %s excluído.", orcamento_id)
    except sqlite3.Error:
        logger.exception("Falha ao excluir o orçamento %s", orcamento_id)
        raise ContaInvalida("Não foi possível excluir o orçamento.")


# ---- Checkout financeiro ----

def efetuar_checkout(orcamento_id, forma_pagamento, colaborador_id, data_vencimento=None):
    """Transforma um orçamento aprovado em entrada financeira:
    - 'boleto' cria a cobrança pendente em cobrancas_boletos (com vencimento),
      que entra na Régua de Cobrança automática como qualquer boleto;
    - 'avista' registra a cobrança já paga hoje (entra no Recebido do mês).
    O orçamento vira 'faturado' e guarda forma, colaborador e data do checkout."""
    if forma_pagamento not in FORMAS_CHECKOUT:
        raise ContaInvalida("Escolha a forma de pagamento: à vista ou boleto.")

    if not (str(colaborador_id) or "").strip().isdigit():
        raise ContaInvalida("Informe o colaborador responsável pelo checkout.")
    colaborador_id = int(colaborador_id)

    hoje = date.today()
    if forma_pagamento == "boleto":
        try:
            vencimento = datetime.strptime(data_vencimento or "", "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ContaInvalida("Informe a data de vencimento do boleto.")
        if vencimento < hoje:
            raise ContaInvalida("O vencimento do boleto não pode estar no passado.")
    else:
        vencimento = hoje

    try:
        with get_connection() as conexao:
            orcamento = conexao.execute(
                "SELECT o.id, o.status, o.valor_total, p.nome, p.telefone "
                "FROM orcamentos o JOIN pacientes p ON p.id = o.paciente_id WHERE o.id = ?",
                (orcamento_id,),
            ).fetchone()
            if not orcamento:
                raise ContaInvalida("Orçamento não encontrado.")
            if orcamento["status"] != "aprovado":
                raise ContaInvalida("Só orçamentos aprovados passam pelo checkout.")
            if not conexao.execute(
                "SELECT 1 FROM colaboradores WHERE id = ?", (colaborador_id,)
            ).fetchone():
                raise ContaInvalida("Colaborador não encontrado.")

            if forma_pagamento == "boleto":
                conexao.execute(
                    "INSERT INTO cobrancas_boletos "
                    "(nome_cliente, telefone, valor, data_vencimento, status, orcamento_id) "
                    "VALUES (?, ?, ?, ?, 'pendente', ?)",
                    (orcamento["nome"], orcamento["telefone"], orcamento["valor_total"],
                     vencimento.isoformat(), orcamento_id),
                )
            else:
                conexao.execute(
                    "INSERT INTO cobrancas_boletos "
                    "(nome_cliente, telefone, valor, data_vencimento, status, data_pagamento, orcamento_id) "
                    "VALUES (?, ?, ?, ?, 'pago', ?, ?)",
                    (orcamento["nome"], orcamento["telefone"], orcamento["valor_total"],
                     vencimento.isoformat(), hoje.isoformat(), orcamento_id),
                )

            conexao.execute(
                "UPDATE orcamentos SET status = 'faturado', checkout_forma = ?, "
                "checkout_colaborador_id = ?, checkout_em = ? WHERE id = ?",
                (forma_pagamento, colaborador_id, hoje.isoformat(), orcamento_id),
            )
        logger.info(
            "Checkout do orçamento %s: %s de R$ %s para '%s'.",
            orcamento_id, FORMAS_CHECKOUT[forma_pagamento],
            formatar_moeda(orcamento["valor_total"]), orcamento["nome"],
        )
    except sqlite3.Error:
        logger.exception("Falha no checkout do orçamento %s", orcamento_id)
        raise ContaInvalida("Não foi possível concluir o checkout.")


def resumo_financeiro_paciente(paciente_id):
    """Cards da sub-aba Financeiro do prontuário: total aguardando aprovação,
    aprovado esperando checkout e total já faturado."""
    with get_connection() as conexao:
        linhas = conexao.execute(
            "SELECT status, SUM(valor_total) AS total, COUNT(*) AS qtd "
            "FROM orcamentos WHERE paciente_id = ? GROUP BY status",
            (paciente_id,),
        ).fetchall()
    resumo = {status: {"total": 0.0, "qtd": 0} for status in STATUS_ORCAMENTO_LABELS}
    for l in linhas:
        if l["status"] in resumo:
            resumo[l["status"]] = {"total": l["total"] or 0.0, "qtd": l["qtd"]}
    return resumo
