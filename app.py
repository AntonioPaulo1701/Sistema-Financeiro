import logging
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

import automacao
import config
import database
import extrato
import services
import whatsapp

if sys.platform == "win32":
    # Evita acentos corrompidos nos logs do terminal do Windows.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads" / "faturas"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

COMPROVANTES_DIR = Path(__file__).resolve().parent / "uploads" / "comprovantes"
COMPROVANTES_DIR.mkdir(parents=True, exist_ok=True)

EXTRATOS_DIR = Path(__file__).resolve().parent / "uploads" / "extratos"
EXTRATOS_DIR.mkdir(parents=True, exist_ok=True)

FATURAS_OFX_DIR = Path(__file__).resolve().parent / "uploads" / "faturas_ofx"
FATURAS_OFX_DIR.mkdir(parents=True, exist_ok=True)

FOTOS_PACIENTES_DIR = Path(__file__).resolve().parent / "uploads" / "fotos_pacientes"
FOTOS_PACIENTES_DIR.mkdir(parents=True, exist_ok=True)

EXTENSOES_COMPROVANTE = {".pdf", ".png", ".jpg", ".jpeg"}
EXTENSOES_FOTO = {".png", ".jpg", ".jpeg"}

app = Flask(__name__)
app.jinja_env.filters["brl"] = services.formatar_moeda
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB por upload


@app.context_processor
def injetar_marca():
    """Deixa o nome da clínica disponível em todos os templates."""
    return {"NOME_CLINICA": config.NOME_CLINICA}


database.init_db()


def _parse_data(data_texto):
    try:
        return datetime.strptime(data_texto, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date.today()


def _parse_periodo(args):
    """Resolve o período do relatório a partir dos parâmetros da URL.

    Aceita um mês inteiro (mes=YYYY-MM) ou um intervalo livre (inicio/fim).
    Sem parâmetros, o período é o dia de hoje. Datas invertidas são corrigidas.
    """
    mes = args.get("mes")
    if mes:
        try:
            primeiro_dia = datetime.strptime(mes, "%Y-%m").date()
            if primeiro_dia.month == 12:
                ultimo_dia = primeiro_dia.replace(year=primeiro_dia.year + 1, month=1, day=1)
            else:
                ultimo_dia = primeiro_dia.replace(month=primeiro_dia.month + 1, day=1)
            ultimo_dia = ultimo_dia - timedelta(days=1)
            return primeiro_dia, ultimo_dia
        except (TypeError, ValueError):
            logger.warning("Mês inválido recebido no relatório: %s", mes)

    inicio = _parse_data(args.get("inicio") or args.get("data"))
    fim = _parse_data(args.get("fim") or args.get("inicio") or args.get("data"))
    if fim < inicio:
        inicio, fim = fim, inicio
    return inicio, fim


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        metricas=services.obter_metricas(),
        categorias=services.obter_categorias(),
        contas=services.listar_contas()[:5],
        gargalos=services.detectar_gargalos_financeiros(),
    )


def _carregar_transacoes_extrato(token):
    """Transações do extrato enviado (identificado pelo token), com a marcação
    das que já viraram conta no sistema. Token inválido devolve (None, None)."""
    if not token:
        return None, None
    nome_base = secure_filename(token)
    caminho = next(
        (EXTRATOS_DIR / f"{nome_base}{ext}" for ext in (".ofx", ".csv")
         if (EXTRATOS_DIR / f"{nome_base}{ext}").exists()),
        None,
    )
    if caminho is None:
        return None, None
    try:
        transacoes = extrato.ler_extrato(caminho)
    except extrato.LeituraExtratoErro:
        logger.warning("Extrato %s não pôde mais ser lido.", caminho.name)
        return None, None

    for t in transacoes:
        t["importada"] = services.conta_ja_existe(t["descricao"], t["valor"], t["data"])
        ano, mes, dia = t["data"].split("-")
        t["data_br"] = f"{dia}/{mes}/{ano}"
    return token, transacoes


def _render_contas(erro=None, codigo=200, extrato_token=None):
    token, transacoes = _carregar_transacoes_extrato(
        extrato_token or request.args.get("extrato")
    )
    return (
        render_template(
            "contas.html",
            active_page="contas",
            contas=services.listar_contas(),
            fornecedores=services.listar_fornecedores(),
            categorias=services.listar_categorias(),
            contas_bancarias=services.listar_contas_bancarias(),
            extrato_token=token,
            transacoes=transacoes,
            erro=erro,
        ),
        codigo,
    )


def _carregar_transacoes_fatura_ofx(token, cartao_id):
    """Compras da fatura OFX enviada (identificada pelo token) + o cartão dela.

    Marca as compras que já viraram lançamento. Token/cartão inválidos → (None, None, None)."""
    if not token or not cartao_id:
        return None, None, None
    cartao = next((c for c in services.listar_cartoes() if str(c["id"]) == str(cartao_id)), None)
    caminho = FATURAS_OFX_DIR / f"{secure_filename(token)}.ofx"
    if cartao is None or not caminho.exists():
        return None, None, None
    try:
        transacoes = extrato.ler_fatura_cartao(caminho)
    except extrato.LeituraExtratoErro:
        logger.warning("Fatura OFX %s não pôde mais ser lida.", caminho.name)
        return None, None, None

    for t in transacoes:
        t["importada"] = services.conta_ja_existe(t["descricao"], t["valor"], t["data"])
        ano, mes, dia = t["data"].split("-")
        t["data_br"] = f"{dia}/{mes}/{ano}"
    return token, cartao, transacoes


def _render_cartao(erro=None, codigo=200, fatura_ofx_token=None, fatura_ofx_cartao=None):
    token, cartao_ofx, transacoes_ofx = _carregar_transacoes_fatura_ofx(
        fatura_ofx_token or request.args.get("fatura_ofx"),
        fatura_ofx_cartao or request.args.get("cartao"),
    )
    filtro_cartao = request.args.get("filtro_cartao") or None
    return (
        render_template(
            "cartao.html",
            active_page="cartao",
            faturas=services.listar_faturas(cartao_id=filtro_cartao),
            filtro_cartao=filtro_cartao,
            fornecedores=services.listar_fornecedores(),
            categorias=services.listar_categorias(),
            contas_bancarias=services.listar_contas_bancarias(),
            cartoes=services.listar_cartoes(),
            fatura_ofx_token=token,
            cartao_ofx=cartao_ofx,
            transacoes_ofx=transacoes_ofx,
            erro=erro,
        ),
        codigo,
    )


def _render_cadastros(erro=None, codigo=200, aba=None):
    return (
        render_template(
            "cadastros.html",
            active_page="cadastros",
            fornecedores=services.listar_fornecedores(),
            categorias=services.listar_categorias(),
            contas_bancarias=services.listar_contas_bancarias(),
            cartoes=services.listar_cartoes(),
            procedimentos=services.listar_procedimentos(),
            colaboradores=services.listar_colaboradores(),
            aba_ativa=aba or request.args.get("aba") or "plano",
            erro=erro,
        ),
        codigo,
    )


@app.route("/cartoes/novo", methods=["POST"])
def novo_cartao():
    try:
        services.adicionar_cartao(request.form.get("nome"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de cartão rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="cartoes")
    return redirect(url_for("cadastros", aba="cartoes"))


@app.route("/cartoes/<int:cartao_id>/excluir", methods=["POST"])
def excluir_cartao(cartao_id):
    try:
        services.remover_cartao(cartao_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover cartão %s: %s", cartao_id, erro)
    return redirect(url_for("cadastros", aba="cartoes"))


@app.route("/cadastros")
def cadastros():
    return _render_cadastros()


@app.route("/plano-contas/nova", methods=["POST"])
def nova_conta_bancaria():
    try:
        services.adicionar_conta_bancaria(request.form.get("nome"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro no plano de contas rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="plano")
    return redirect(url_for("cadastros", aba="plano"))


@app.route("/plano-contas/<int:conta_bancaria_id>/excluir", methods=["POST"])
def excluir_conta_bancaria(conta_bancaria_id):
    try:
        services.remover_conta_bancaria(conta_bancaria_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover conta bancária %s: %s", conta_bancaria_id, erro)
    return redirect(url_for("cadastros", aba="plano"))


@app.route("/categorias/nova", methods=["POST"])
def nova_categoria():
    try:
        services.adicionar_categoria(request.form.get("nome"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de categoria rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="categorias")
    return redirect(url_for("cadastros", aba="categorias"))


@app.route("/categorias/<int:categoria_id>/excluir", methods=["POST"])
def excluir_categoria(categoria_id):
    try:
        services.remover_categoria(categoria_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover categoria %s: %s", categoria_id, erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="categorias")
    return redirect(url_for("cadastros", aba="categorias"))


@app.route("/procedimentos/novo", methods=["POST"])
def novo_procedimento():
    try:
        services.adicionar_procedimento(request.form.get("nome"), request.form.get("valor"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de procedimento rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="procedimentos")
    return redirect(url_for("cadastros", aba="procedimentos"))


@app.route("/procedimentos/<int:procedimento_id>/excluir", methods=["POST"])
def excluir_procedimento(procedimento_id):
    try:
        services.remover_procedimento(procedimento_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover procedimento %s: %s", procedimento_id, erro)
    return redirect(url_for("cadastros", aba="procedimentos"))


@app.route("/colaboradores/novo", methods=["POST"])
def novo_colaborador():
    try:
        services.adicionar_colaborador(request.form.get("nome"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de colaborador rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="colaboradores")
    return redirect(url_for("cadastros", aba="colaboradores"))


@app.route("/colaboradores/<int:colaborador_id>/excluir", methods=["POST"])
def excluir_colaborador(colaborador_id):
    try:
        services.remover_colaborador(colaborador_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover colaborador %s: %s", colaborador_id, erro)
    return redirect(url_for("cadastros", aba="colaboradores"))


# ---------------------------------------------------------------------------
# Prontuário Digital do Paciente
# ---------------------------------------------------------------------------

def _render_pacientes(erro=None, codigo=200):
    return (
        render_template(
            "pacientes.html",
            active_page="pacientes",
            pacientes=services.listar_pacientes(),
            erro=erro,
        ),
        codigo,
    )


def _render_prontuario(paciente_id, aba=None, erro=None, codigo=200):
    return (
        render_template(
            "prontuario.html",
            active_page="pacientes",
            paciente=services.obter_paciente(paciente_id),
            orcamentos=services.listar_orcamentos_paciente(paciente_id),
            procedimentos=services.listar_procedimentos(),
            colaboradores=services.listar_colaboradores(),
            resumo_financeiro=services.resumo_financeiro_paciente(paciente_id),
            aba_ativa=aba or request.args.get("aba") or "cadastro",
            erro=erro,
        ),
        codigo,
    )


@app.route("/pacientes")
def pacientes():
    return _render_pacientes()


@app.route("/pacientes/novo", methods=["POST"])
def novo_paciente():
    try:
        paciente_id = services.criar_paciente(
            request.form.get("nome"),
            request.form.get("telefone"),
            request.form.get("cpf"),
            request.form.get("data_nascimento"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de paciente rejeitado: %s", erro)
        return _render_pacientes(erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id))


@app.route("/pacientes/prontuario/<int:paciente_id>")
def prontuario(paciente_id):
    try:
        return _render_prontuario(paciente_id)
    except services.ContaInvalida as erro:
        logger.warning("Prontuário indisponível (%s): %s", paciente_id, erro)
        return redirect(url_for("pacientes"))


@app.route("/pacientes/<int:paciente_id>/editar", methods=["POST"])
def editar_paciente(paciente_id):
    try:
        services.editar_paciente(
            paciente_id,
            request.form.get("nome"),
            request.form.get("telefone"),
            request.form.get("cpf"),
            request.form.get("data_nascimento"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Edição do paciente %s rejeitada: %s", paciente_id, erro)
        return _render_prontuario(paciente_id, aba="cadastro", erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="cadastro"))


@app.route("/pacientes/<int:paciente_id>/excluir", methods=["POST"])
def excluir_paciente(paciente_id):
    try:
        services.excluir_paciente(paciente_id)
    except services.ContaInvalida as erro:
        logger.warning("Exclusão do paciente %s rejeitada: %s", paciente_id, erro)
        return _render_prontuario(paciente_id, aba="cadastro", erro=str(erro), codigo=400)
    return redirect(url_for("pacientes"))


@app.route("/pacientes/<int:paciente_id>/orcamentos/novo", methods=["POST"])
def novo_orcamento(paciente_id):
    try:
        services.criar_orcamento(
            paciente_id,
            request.form.getlist("procedimentos"),
            request.form.get("colaborador_id"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Orçamento rejeitado para o paciente %s: %s", paciente_id, erro)
        return _render_prontuario(paciente_id, aba="orcamentos", erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="orcamentos"))


@app.route("/pacientes/<int:paciente_id>/orcamentos/<int:orcamento_id>/aprovar", methods=["POST"])
def aprovar_orcamento(paciente_id, orcamento_id):
    try:
        services.aprovar_orcamento(orcamento_id)
    except services.ContaInvalida as erro:
        logger.warning("Aprovação do orçamento %s rejeitada: %s", orcamento_id, erro)
        return _render_prontuario(paciente_id, aba="orcamentos", erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="orcamentos"))


@app.route("/pacientes/<int:paciente_id>/orcamentos/<int:orcamento_id>/excluir", methods=["POST"])
def excluir_orcamento(paciente_id, orcamento_id):
    try:
        services.excluir_orcamento(orcamento_id)
    except services.ContaInvalida as erro:
        logger.warning("Exclusão do orçamento %s rejeitada: %s", orcamento_id, erro)
        return _render_prontuario(paciente_id, aba="orcamentos", erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="orcamentos"))


@app.route("/pacientes/<int:paciente_id>/orcamentos/<int:orcamento_id>/checkout", methods=["POST"])
def checkout_orcamento(paciente_id, orcamento_id):
    try:
        services.efetuar_checkout(
            orcamento_id,
            request.form.get("forma_pagamento"),
            request.form.get("colaborador_id"),
            request.form.get("data_vencimento"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Checkout do orçamento %s rejeitado: %s", orcamento_id, erro)
        return _render_prontuario(paciente_id, aba="financeiro", erro=str(erro), codigo=400)
    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="financeiro"))


@app.route("/pacientes/<int:paciente_id>/foto", methods=["POST"])
def enviar_foto_paciente(paciente_id):
    arquivo = request.files.get("foto")
    if not arquivo or not arquivo.filename:
        return _render_prontuario(paciente_id, aba="cadastro",
                                  erro="Escolha o arquivo da foto para enviar.", codigo=400)

    extensao = Path(arquivo.filename).suffix.lower()
    if extensao not in EXTENSOES_FOTO:
        return _render_prontuario(paciente_id, aba="cadastro",
                                  erro="Formato não aceito. Envie a foto em PNG ou JPG.", codigo=400)

    nome_arquivo = secure_filename(f"paciente_{paciente_id}{extensao}")
    try:
        foto_antiga = services.obter_foto_paciente(paciente_id)
        arquivo.save(FOTOS_PACIENTES_DIR / nome_arquivo)
        services.salvar_foto_paciente(paciente_id, nome_arquivo)
        if foto_antiga and foto_antiga != nome_arquivo:
            # Limpeza da foto anterior (extensão diferente). Nunca é fatal: o
            # OneDrive/Windows pode segurar o arquivo por instantes e o órfão
            # não atrapalha — o banco já aponta para a foto nova.
            try:
                (FOTOS_PACIENTES_DIR / foto_antiga).unlink(missing_ok=True)
            except OSError:
                logger.warning("Foto antiga '%s' ficou para trás (arquivo em uso).", foto_antiga)
        logger.info("Foto do paciente %s salva em %s", paciente_id, FOTOS_PACIENTES_DIR / nome_arquivo)
    except services.ContaInvalida as erro:
        return _render_prontuario(paciente_id, aba="cadastro", erro=str(erro), codigo=400)
    except OSError:
        logger.exception("Falha ao salvar a foto do paciente %s", paciente_id)
        return _render_prontuario(paciente_id, aba="cadastro",
                                  erro="Não foi possível salvar o arquivo da foto.", codigo=500)

    return redirect(url_for("prontuario", paciente_id=paciente_id, aba="cadastro"))


@app.route("/pacientes/<int:paciente_id>/foto")
def ver_foto_paciente(paciente_id):
    nome_arquivo = services.obter_foto_paciente(paciente_id)
    if not nome_arquivo:
        return ("", 404)
    # max_age=0: trocar a foto atualiza o avatar na hora (sem cache velho).
    return send_from_directory(FOTOS_PACIENTES_DIR, nome_arquivo, max_age=0)


@app.route("/api/pesquisa_pacientes")
def pesquisa_pacientes():
    """Barra de Pesquisa Global: busca dinâmica por nome ou CPF, em JSON."""
    return jsonify(services.pesquisar_pacientes(request.args.get("q", "")))


@app.route("/contas")
def contas():
    return _render_contas()


@app.route("/contas/nova", methods=["POST"])
def nova_conta():
    # A aba Cartão de Crédito tem seu próprio formulário de lançamento; quando o
    # cadastro vem de lá, o erro e o redirect voltam para aquela tela.
    origem_cartao = request.form.get("origem") == "cartao"
    try:
        services.criar_conta(
            nome=request.form.get("nome"),
            valor=request.form.get("valor"),
            vencimento=request.form.get("vencimento"),
            categoria=request.form.get("categoria"),
            forma_pagamento=request.form.get("forma_pagamento", "outro"),
            fornecedor_id=request.form.get("fornecedor_id"),
            parcelas=request.form.get("parcelas", 1),
            conta_bancaria_id=request.form.get("conta_bancaria_id"),
            recorrencia=request.form.get("recorrencia", 1),
            cartao_id=request.form.get("cartao_id"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de conta rejeitado: %s", erro)
        if origem_cartao:
            return _render_cartao(erro=str(erro), codigo=400)
        return _render_contas(erro=str(erro), codigo=400)
    return redirect(url_for("cartao") if origem_cartao else url_for("contas"))


@app.route("/contas/extrato", methods=["POST"])
def importar_extrato():
    """Recebe o arquivo .ofx/.csv, valida a leitura e abre a tabela de transações."""
    arquivo = request.files.get("extrato")
    if not arquivo or not arquivo.filename:
        return _render_contas(erro="Escolha o arquivo do extrato (.ofx ou .csv) para enviar.", codigo=400)

    extensao = Path(arquivo.filename).suffix.lower()
    if extensao not in extrato.EXTENSOES_ACEITAS:
        return _render_contas(erro="Formato não aceito. Envie o extrato em .ofx ou .csv.", codigo=400)

    token = uuid.uuid4().hex[:12]
    caminho = EXTRATOS_DIR / f"{token}{extensao}"
    try:
        arquivo.save(caminho)
        transacoes = extrato.ler_extrato(caminho)  # valida já na chegada
        logger.info("Extrato importado com token %s: %d transação(ões).", token, len(transacoes))
    except extrato.LeituraExtratoErro as erro:
        caminho.unlink(missing_ok=True)
        logger.warning("Extrato rejeitado: %s", erro)
        return _render_contas(erro=str(erro), codigo=400)
    except OSError:
        logger.exception("Falha ao salvar o extrato enviado")
        return _render_contas(erro="Não foi possível salvar o arquivo do extrato.", codigo=500)

    # Redirect com o token: a tabela sobrevive a recarregamentos e a cada salvamento.
    return redirect(url_for("contas", extrato=token))


@app.route("/contas/extrato/salvar", methods=["POST"])
def salvar_transacao_extrato():
    """Salva uma transação do extrato como conta, em um clique."""
    token = request.form.get("token", "")
    try:
        services.criar_conta(
            nome=request.form.get("descricao"),
            valor=request.form.get("valor"),
            vencimento=request.form.get("data"),
            categoria=request.form.get("categoria"),
            forma_pagamento="outro",
            ja_paga=True,  # o extrato mostra dinheiro que JÁ saiu da conta
        )
    except services.ContaInvalida as erro:
        logger.warning("Transação do extrato rejeitada: %s", erro)
        return _render_contas(erro=str(erro), codigo=400, extrato_token=token)
    return redirect(url_for("contas", extrato=token))


@app.route("/fornecedores/novo", methods=["POST"])
def novo_fornecedor():
    try:
        services.adicionar_fornecedor(request.form.get("nome"), request.form.get("telefone"))
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de fornecedor rejeitado: %s", erro)
        return _render_cadastros(erro=str(erro), codigo=400, aba="fornecedores")
    return redirect(url_for("cadastros", aba="fornecedores"))


@app.route("/fornecedores/<int:fornecedor_id>/excluir", methods=["POST"])
def excluir_fornecedor(fornecedor_id):
    try:
        services.remover_fornecedor(fornecedor_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover fornecedor %s: %s", fornecedor_id, erro)
    return redirect(url_for("cadastros", aba="fornecedores"))


@app.route("/contas/<int:conta_id>/comprovante", methods=["POST"])
def enviar_comprovante(conta_id):
    arquivo = request.files.get("comprovante")
    if not arquivo or not arquivo.filename:
        return _render_contas(erro="Escolha o arquivo do comprovante para enviar.", codigo=400)

    extensao = Path(arquivo.filename).suffix.lower()
    if extensao not in EXTENSOES_COMPROVANTE:
        return _render_contas(
            erro="Formato não aceito. Envie o comprovante em PDF, PNG ou JPG.", codigo=400
        )

    nome_arquivo = secure_filename(f"conta_{conta_id}{extensao}")
    try:
        arquivo.save(COMPROVANTES_DIR / nome_arquivo)
        services.salvar_comprovante(conta_id, nome_arquivo)
        logger.info("Comprovante da conta %s salvo em %s", conta_id, COMPROVANTES_DIR / nome_arquivo)
    except services.ContaInvalida as erro:
        return _render_contas(erro=str(erro), codigo=400)
    except OSError:
        logger.exception("Falha ao salvar o comprovante da conta %s", conta_id)
        return _render_contas(erro="Não foi possível salvar o arquivo do comprovante.", codigo=500)

    return redirect(url_for("contas"))


@app.route("/contas/<int:conta_id>/comprovante")
def ver_comprovante(conta_id):
    nome_arquivo = services.obter_comprovante(conta_id)
    if not nome_arquivo:
        return _render_contas(erro="Esta conta ainda não tem comprovante anexado.", codigo=404)
    return send_from_directory(COMPROVANTES_DIR, nome_arquivo)


@app.route("/contas/<int:conta_id>/status", methods=["POST"])
def alternar_status_conta(conta_id):
    try:
        services.alternar_status(conta_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao alternar status da conta %s: %s", conta_id, erro)
    return redirect(url_for("contas"))


@app.route("/contas/<int:conta_id>/excluir", methods=["POST"])
def remover_conta(conta_id):
    try:
        services.excluir_conta(conta_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao excluir a conta %s: %s", conta_id, erro)
    return redirect(url_for("contas"))


def _render_boletos(erro=None, codigo=200, status_envio=None):
    metricas = services.obter_metricas_boletos()
    return (
        render_template(
            "boletos.html",
            active_page="boletos",
            boletos=services.listar_boletos(),
            metricas=metricas,
            meta=services.montar_progresso_meta(metricas["recebido"], metricas["mes_label"]),
            historico=services.obter_historico_recebimentos(),
            dias_lembrete=services.obter_dias_lembrete(),
            intervalo_cobranca=services.obter_intervalo_cobranca(),
            erro=erro,
            status_envio=status_envio,
        ),
        codigo,
    )


@app.route("/boletos")
def boletos():
    return _render_boletos()


@app.route("/boletos/novo", methods=["POST"])
def novo_boleto():
    try:
        services.criar_boleto(
            nome_cliente=request.form.get("nome_cliente"),
            telefone=request.form.get("telefone"),
            valor=request.form.get("valor"),
            data_vencimento=request.form.get("data_vencimento"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Cadastro de boleto rejeitado: %s", erro)
        return _render_boletos(erro=str(erro), codigo=400)
    return redirect(url_for("boletos"))


@app.route("/boletos/regua", methods=["POST"])
def salvar_regua():
    """Salva a configuração da régua: dias do lembrete prévio e intervalo da cobrança."""
    try:
        lembrete, intervalo = services.salvar_config_regua(
            request.form.get("dias_lembrete"),
            request.form.get("intervalo_cobranca"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Configuração da régua rejeitada: %s", erro)
        return _render_boletos(status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    ritmo = "todos os dias" if intervalo == 1 else f"a cada {intervalo} dias"
    status_envio = {
        "tipo": "sucesso",
        "texto": (
            f"Régua atualizada! Lembrete {lembrete} dia(s) antes do vencimento e cobrança {ritmo} "
            "após vencer, a partir da próxima varredura diária."
        ),
    }
    return _render_boletos(status_envio=status_envio)


@app.route("/boletos/<int:boleto_id>/agendar-mensagem", methods=["POST"])
def agendar_mensagem(boleto_id):
    """Salva o recado customizado do boleto, disparado pelo envio diário na data marcada."""
    try:
        nome, data_envio = services.agendar_mensagem_boleto(
            boleto_id,
            request.form.get("mensagem_data"),
            request.form.get("mensagem_texto"),
        )
    except services.ContaInvalida as erro:
        logger.warning("Agendamento de recado rejeitado (boleto %s): %s", boleto_id, erro)
        return _render_boletos(status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    status_envio = {
        "tipo": "sucesso",
        "texto": (
            f"Recado para {nome} agendado! Será enviado pelo WhatsApp no envio "
            f"automático de {data_envio.strftime('%d/%m/%Y')}."
        ),
    }
    return _render_boletos(status_envio=status_envio)


@app.route("/boletos/<int:boleto_id>/cancelar-mensagem", methods=["POST"])
def cancelar_mensagem(boleto_id):
    """Remove o recado agendado do boleto."""
    try:
        services.cancelar_mensagem_boleto(boleto_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover recado do boleto %s: %s", boleto_id, erro)
        return _render_boletos(status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    return _render_boletos(
        status_envio={"tipo": "sucesso", "texto": "Recado agendado removido. Nada será enviado."}
    )


@app.route("/boletos/meta", methods=["POST"])
def salvar_meta():
    """Salva a Meta de Arrecadação Mensal exibida na barra de progresso."""
    try:
        meta = services.salvar_meta_mensal(request.form.get("meta_mensal"))
    except services.ContaInvalida as erro:
        logger.warning("Meta de arrecadação rejeitada: %s", erro)
        return _render_boletos(status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    if meta > 0:
        texto = f"Meta de arrecadação atualizada para R$ {services.formatar_moeda(meta)} por mês."
    else:
        texto = "Meta de arrecadação desligada. A barra de progresso saiu da tela."
    return _render_boletos(status_envio={"tipo": "sucesso", "texto": texto})


@app.route("/boletos/cobrar", methods=["POST"])
def cobrar_boletos():
    """Dispara agora a cobrança de todos os boletos vencidos pelo WhatsApp."""
    qtd = services.cobrar_boletos_vencidos_em_segundo_plano()
    if not qtd:
        status_envio = {
            "tipo": "erro",
            "texto": "Nenhum boleto vencido para cobrar agora — ou todos já receberam a mensagem de hoje.",
        }
        return _render_boletos(status_envio=status_envio)
    minutos_total = 2 * (qtd - 1)
    status_envio = {
        "tipo": "sucesso",
        "texto": (
            f"Cobrança iniciada para {qtd} boleto(s) vencido(s) — uma mensagem a cada 2 minutos"
            + (f" (termina em ~{minutos_total} min)" if qtd > 1 else "")
            + ". Deixe o computador ligado e o WhatsApp Web logado até o fim."
        ),
    }
    return _render_boletos(status_envio=status_envio)


@app.route("/boletos/<int:boleto_id>/status", methods=["POST"])
def alternar_status_boleto(boleto_id):
    try:
        services.alternar_status_boleto(boleto_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao alternar status do boleto %s: %s", boleto_id, erro)
    return redirect(url_for("boletos"))


@app.route("/boletos/<int:boleto_id>/excluir", methods=["POST"])
def remover_boleto(boleto_id):
    try:
        services.excluir_boleto(boleto_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao excluir o boleto %s: %s", boleto_id, erro)
    return redirect(url_for("boletos"))


def _status_automacao():
    """Situação do envio automático diário exibida na Central de Relatórios."""
    try:
        ativa = automacao.tarefa_ativa()
    except automacao.AutomacaoErro:
        ativa = False
    return {
        "ativa": ativa,
        "hora": services.obter_config("hora_envio_automatico"),
    }


def _render_relatorios(inicio, fim, texto=None, status_envio=None, codigo=200, auditoria=None):
    return (
        render_template(
            "relatorios.html",
            active_page="relatorios",
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
            periodo_de_um_dia=(inicio == fim),
            resumo=services.obter_resumo_periodo(inicio, fim),
            ofensores=services.obter_maiores_ofensores(inicio, fim),
            texto_relatorio=texto if texto is not None else services.gerar_relatorio_texto(inicio, fim),
            destinatarios=services.listar_destinatarios(),
            status_envio=status_envio,
            automacao_info=_status_automacao(),
            auditoria=auditoria,
        ),
        codigo,
    )


@app.route("/relatorios")
def relatorios():
    inicio, fim = _parse_periodo(request.args)
    return _render_relatorios(inicio, fim)


@app.route("/relatorios/auditar", methods=["POST"])
def auditar_financas():
    inicio, fim = _parse_periodo(request.form)
    try:
        auditoria = services.auditar_financas_ia()
    except services.ContaInvalida as erro:
        logger.warning("Auditoria com IA falhou: %s", erro)
        return _render_relatorios(inicio, fim, status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    return _render_relatorios(inicio, fim, auditoria=auditoria)


@app.route("/relatorios/enviar", methods=["POST"])
def enviar_relatorio():
    inicio, fim = _parse_periodo(request.form)
    texto = request.form.get("texto", "")

    numeros = [d["numero"] for d in services.listar_destinatarios()]
    if not numeros:
        status_envio = {
            "tipo": "erro",
            "texto": "Nenhum número cadastrado. Cadastre pelo menos um número no painel 'Números do WhatsApp'.",
        }
        return _render_relatorios(inicio, fim, texto=texto, status_envio=status_envio, codigo=400)

    if len(numeros) == 1:
        try:
            whatsapp.enviar_mensagem(numeros[0], texto)
            status_envio = {"tipo": "sucesso", "texto": f"Relatório enviado para {numeros[0]} com sucesso!"}
        except whatsapp.EnvioWhatsAppInvalido as erro:
            logger.warning("Falha ao enviar relatório via WhatsApp: %s", erro)
            status_envio = {"tipo": "erro", "texto": str(erro)}
    else:
        if not (texto or "").strip():
            return _render_relatorios(
                inicio, fim, texto=texto,
                status_envio={"tipo": "erro", "texto": "O texto do relatório está vazio."},
                codigo=400,
            )
        whatsapp.enviar_para_varios_em_segundo_plano(numeros, texto)
        minutos_total = 2 * (len(numeros) - 1)
        status_envio = {
            "tipo": "sucesso",
            "texto": (
                f"Envio iniciado para {len(numeros)} números — um a cada 2 minutos "
                f"(termina em ~{minutos_total} min). Deixe o computador ligado e o sistema aberto até o fim."
            ),
        }

    return _render_relatorios(inicio, fim, texto=texto, status_envio=status_envio)


@app.route("/destinatarios/novo", methods=["POST"])
def novo_destinatario():
    inicio, fim = _parse_periodo(request.form)
    try:
        services.adicionar_destinatario(request.form.get("apelido"), request.form.get("numero"))
    except (services.ContaInvalida, whatsapp.EnvioWhatsAppInvalido) as erro:
        logger.warning("Cadastro de destinatário rejeitado: %s", erro)
        return _render_relatorios(inicio, fim, status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)
    return redirect(url_for("relatorios", inicio=inicio.isoformat(), fim=fim.isoformat()))


@app.route("/destinatarios/<int:destinatario_id>/excluir", methods=["POST"])
def excluir_destinatario(destinatario_id):
    inicio, fim = _parse_periodo(request.form)
    try:
        services.remover_destinatario(destinatario_id)
    except services.ContaInvalida as erro:
        logger.warning("Falha ao remover destinatário %s: %s", destinatario_id, erro)
    return redirect(url_for("relatorios", inicio=inicio.isoformat(), fim=fim.isoformat()))


@app.route("/automacao/ativar", methods=["POST"])
def ativar_automacao():
    inicio, fim = _parse_periodo(request.form)
    hora = request.form.get("hora_automatica", "")

    if not services.listar_destinatarios():
        status_envio = {
            "tipo": "erro",
            "texto": "Cadastre pelo menos um número no painel 'Números do WhatsApp' antes de ativar o envio automático.",
        }
        return _render_relatorios(inicio, fim, status_envio=status_envio, codigo=400)

    try:
        automacao.ativar_envio_diario(hora)
        services.salvar_config("hora_envio_automatico", hora)
        status_envio = {
            "tipo": "sucesso",
            "texto": (
                f"Envio automático ativado! Todos os dias às {hora} o relatório do dia será enviado "
                "para todos os números cadastrados."
            ),
        }
    except automacao.AutomacaoErro as erro:
        logger.warning("Falha ao ativar envio automático: %s", erro)
        return _render_relatorios(inicio, fim, status_envio={"tipo": "erro", "texto": str(erro)}, codigo=400)

    return _render_relatorios(inicio, fim, status_envio=status_envio)


@app.route("/automacao/desativar", methods=["POST"])
def desativar_automacao():
    inicio, fim = _parse_periodo(request.form)
    try:
        automacao.desativar_envio_diario()
        status_envio = {"tipo": "sucesso", "texto": "Envio automático desativado."}
    except automacao.AutomacaoErro as erro:
        logger.warning("Falha ao desativar envio automático: %s", erro)
        return _render_relatorios(inicio, fim, status_envio={"tipo": "erro", "texto": str(erro)}, codigo=500)

    return _render_relatorios(inicio, fim, status_envio=status_envio)


def _render_oraculo(simulacao=None, erro=None, codigo=200, form=None):
    categorias = [c["nome"] for c in services.listar_categorias()]
    if not categorias:
        categorias = sorted(services.CATEGORIAS_LABELS.values())
    return (
        render_template(
            "oraculo.html",
            active_page="oraculo",
            projecao=services.projetar_fluxo_caixa(),
            categorias=categorias,
            simulacao=simulacao,
            erro=erro,
            form=form or {},
        ),
        codigo,
    )


@app.route("/oraculo")
def oraculo():
    return _render_oraculo()


@app.route("/oraculo/simular", methods=["POST"])
def simular_oraculo():
    """Roda o Oráculo Financeiro: injeta o gasto simulado na projeção de caixa."""
    form = {campo: request.form.get(campo, "") for campo in ("valor_mensal", "categoria", "duracao_meses")}
    try:
        simulacao = services.simular_cenario_oraculo(
            form["valor_mensal"], form["categoria"], form["duracao_meses"]
        )
    except services.ContaInvalida as erro:
        logger.warning("Simulação do Oráculo rejeitada: %s", erro)
        return _render_oraculo(erro=str(erro), codigo=400, form=form)
    return _render_oraculo(simulacao=simulacao, form=form)


@app.route("/cartao")
def cartao():
    return _render_cartao()


@app.route("/cartao/fatura-ofx", methods=["POST"])
def importar_fatura_ofx():
    """Recebe o OFX da fatura de um cartão e abre a tabela de compras encontradas."""
    cartao_id = request.form.get("cartao_id")
    if not cartao_id:
        return _render_cartao(erro="Escolha de qual cartão é esta fatura.", codigo=400)
    if not any(str(c["id"]) == str(cartao_id) for c in services.listar_cartoes()):
        return _render_cartao(erro="Cartão não encontrado. Cadastre-o na aba Cadastros.", codigo=400)

    arquivo = request.files.get("fatura_ofx")
    if not arquivo or not arquivo.filename:
        return _render_cartao(erro="Escolha o arquivo .ofx da fatura para enviar.", codigo=400)
    if Path(arquivo.filename).suffix.lower() != ".ofx":
        return _render_cartao(erro="Envie a fatura do cartão em .ofx (exportada pelo app do banco).", codigo=400)

    token = uuid.uuid4().hex[:12]
    caminho = FATURAS_OFX_DIR / f"{token}.ofx"
    try:
        arquivo.save(caminho)
        compras = extrato.ler_fatura_cartao(caminho)  # valida já na chegada
        logger.info("Fatura OFX importada (token %s, cartão %s): %d compra(s).", token, cartao_id, len(compras))
    except extrato.LeituraExtratoErro as erro:
        caminho.unlink(missing_ok=True)
        logger.warning("Fatura OFX rejeitada: %s", erro)
        return _render_cartao(erro=str(erro), codigo=400)
    except OSError:
        logger.exception("Falha ao salvar a fatura OFX enviada")
        return _render_cartao(erro="Não foi possível salvar o arquivo da fatura.", codigo=500)

    return redirect(url_for("cartao", fatura_ofx=token, cartao=cartao_id))


@app.route("/cartao/fatura-ofx/lancar", methods=["POST"])
def lancar_compra_fatura_ofx():
    """Lança uma compra da fatura OFX como conta do cartão, em um clique."""
    token = request.form.get("token", "")
    cartao_id = request.form.get("cartao_id", "")
    try:
        services.criar_conta(
            nome=request.form.get("descricao"),
            valor=request.form.get("valor"),
            vencimento=request.form.get("data"),
            categoria=request.form.get("categoria"),
            forma_pagamento="cartao",  # cai na fatura do mês da compra
            cartao_id=cartao_id,
        )
    except services.ContaInvalida as erro:
        logger.warning("Compra da fatura OFX rejeitada: %s", erro)
        return _render_cartao(erro=str(erro), codigo=400,
                              fatura_ofx_token=token, fatura_ofx_cartao=cartao_id)
    return redirect(url_for("cartao", fatura_ofx=token, cartao=cartao_id))


@app.route("/cartao/<referencia>/conciliar", methods=["POST"])
def conciliar_fatura(referencia):
    arquivo = request.files.get("pdf")
    nome_arquivo_salvo = None

    if arquivo and arquivo.filename:
        if not arquivo.filename.lower().endswith(".pdf"):
            logger.warning("Upload rejeitado para fatura %s: arquivo não é PDF (%s)", referencia, arquivo.filename)
            return _render_cartao(erro="Envie o arquivo da fatura em PDF.", codigo=400)
        nome_arquivo_salvo = secure_filename(f"{referencia}.pdf")
        try:
            arquivo.save(UPLOAD_DIR / nome_arquivo_salvo)
            logger.info("PDF da fatura %s salvo em %s", referencia, UPLOAD_DIR / nome_arquivo_salvo)
        except OSError:
            logger.exception("Falha ao salvar o PDF da fatura %s", referencia)
            return _render_cartao(erro="Não foi possível salvar o arquivo enviado.", codigo=500)

    # Sem upload novo, reaproveita o PDF já enviado antes (se existir) para
    # permitir reconciliar depois de cadastrar mais contas no mês.
    caminho_absoluto = None
    if nome_arquivo_salvo:
        caminho_absoluto = UPLOAD_DIR / nome_arquivo_salvo
    else:
        candidato = UPLOAD_DIR / secure_filename(f"{referencia}.pdf")
        if candidato.exists():
            caminho_absoluto = candidato

    try:
        services.conciliar_fatura(
            referencia,
            valor_informado=request.form.get("valor_informado"),
            caminho_pdf=nome_arquivo_salvo,
            caminho_absoluto=caminho_absoluto,
        )
    except services.ContaInvalida as erro:
        logger.warning("Falha ao conciliar fatura %s: %s", referencia, erro)
        return _render_cartao(erro=str(erro), codigo=400)

    return redirect(url_for("cartao"))


@app.route("/cartao/<referencia>/pdf")
def baixar_fatura_pdf(referencia):
    nome_arquivo = secure_filename(f"{referencia}.pdf")
    if not (UPLOAD_DIR / nome_arquivo).exists():
        return _render_cartao(
            erro="O PDF desta fatura não foi encontrado na pasta uploads/faturas. Envie o arquivo novamente.",
            codigo=404,
        )
    return send_from_directory(UPLOAD_DIR, nome_arquivo)


if __name__ == "__main__":
    app.run(debug=True)
