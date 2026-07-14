"""Leitura de extratos bancários (.ofx e .csv) para a importação de despesas.

Extrai apenas as transações de SAÍDA (valores negativos no extrato), devolvendo
data, descrição do banco e valor (sempre positivo, pronto para virar conta).
Nenhuma dependência externa: OFX é lido com expressões regulares e CSV com o
módulo padrão do Python, com heurísticas para os formatos comuns dos bancos
brasileiros (datas dd/mm/aaaa, valores 1.234,56, separador ';' ou ',').
"""

import csv
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

EXTENSOES_ACEITAS = {".ofx", ".csv"}

FORMATOS_DATA = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y")


class LeituraExtratoErro(Exception):
    """Erro ao ler ou interpretar o arquivo de extrato."""


def _ler_texto(caminho):
    """Lê o arquivo tentando as codificações comuns dos bancos (UTF-8 e Latin-1)."""
    dados = caminho.read_bytes()
    for codificacao in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    raise LeituraExtratoErro("Não consegui ler o texto do arquivo (codificação desconhecida).")


def _converter_valor(texto):
    """Converte '1.234,56', '-R$ 50,00' ou '1234.56' em float; None se não for número."""
    texto = (texto or "").strip().replace("R$", "").replace(" ", "")
    if not texto or not re.search(r"\d", texto):
        return None
    negativo = texto.startswith("-") or texto.endswith("-") or texto.startswith("(")
    texto = texto.strip("()+-")
    if "," in texto:
        # Formato brasileiro: ponto de milhar e vírgula decimal.
        texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = float(texto)
    except ValueError:
        return None
    return -valor if negativo else valor


def _converter_data(texto):
    texto = (texto or "").strip()
    for formato in FORMATOS_DATA:
        try:
            return datetime.strptime(texto, formato).date().isoformat()
        except ValueError:
            continue
    return None


def _ler_ofx_bruto(texto):
    """Extrai TODAS as transações dos blocos <STMTTRN> de um OFX (SGML ou XML),
    preservando o sinal do valor (negativo = dinheiro saindo, na maioria dos bancos)."""
    transacoes = []
    for bloco in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|\Z)", texto, re.S | re.I):
        def campo(nome):
            achado = re.search(rf"<{nome}>([^<\r\n]*)", bloco, re.I)
            return achado.group(1).strip() if achado else ""

        valor = _converter_valor(campo("TRNAMT").replace(",", "."))
        data_bruta = campo("DTPOSTED")[:8]
        try:
            data = datetime.strptime(data_bruta, "%Y%m%d").date().isoformat()
        except ValueError:
            data = None
        descricao = campo("MEMO") or campo("NAME") or "Transação sem descrição"

        if valor is None or data is None or valor == 0:
            continue
        transacoes.append({"data": data, "descricao": descricao, "valor": valor})
    return transacoes


def _ler_ofx(texto):
    """Saídas de dinheiro de um OFX de conta corrente (valores negativos)."""
    return [
        {"data": t["data"], "descricao": t["descricao"], "valor": round(-t["valor"], 2)}
        for t in _ler_ofx_bruto(texto)
        if t["valor"] < 0
    ]


def _ler_csv(texto):
    """Lê um CSV de extrato achando, em cada linha, a data, o valor e a descrição.

    Heurística tolerante a cabeçalhos e colunas em ordens diferentes: a primeira
    célula que parecer data vira a data; a última célula numérica vira o valor;
    a descrição é a célula de texto mais longa entre as demais.
    """
    delimitador = ";" if texto.count(";") >= texto.count(",") else ","
    transacoes = []
    for linha in csv.reader(texto.splitlines(), delimiter=delimitador):
        celulas = [c.strip() for c in linha if c.strip()]
        if len(celulas) < 2:
            continue

        data = next((d for d in (_converter_data(c) for c in celulas) if d), None)
        if not data:
            continue  # linha de cabeçalho ou rodapé

        valores = [(c, _converter_valor(c)) for c in celulas]
        numericas = [(c, v) for c, v in valores if v is not None and _converter_data(c) is None]
        # A saída é a primeira célula negativa da linha: em extratos com coluna
        # de saldo, o valor da transação vem antes do saldo do dia.
        negativas = [(c, v) for c, v in numericas if v < 0]
        if not negativas:
            continue  # linha sem saída de dinheiro (entrada ou cabeçalho)
        celula_valor, valor = negativas[0]

        textos = [
            c for c in celulas
            if c != celula_valor and _converter_data(c) is None and _converter_valor(c) is None
        ]
        descricao = max(textos, key=len) if textos else "Transação sem descrição"

        transacoes.append({"data": data, "descricao": descricao, "valor": round(-valor, 2)})
    return transacoes


def ler_extrato(caminho):
    """Lê o arquivo de extrato (Path) e devolve a lista de saídas encontradas.

    Cada item: {'data': 'AAAA-MM-DD', 'descricao': str, 'valor': float > 0}.
    Levanta LeituraExtratoErro quando o arquivo não puder ser interpretado.
    """
    extensao = caminho.suffix.lower()
    if extensao not in EXTENSOES_ACEITAS:
        raise LeituraExtratoErro("Formato não aceito. Envie o extrato em .ofx ou .csv.")

    try:
        texto = _ler_texto(caminho)
    except OSError:
        logger.exception("Falha ao abrir o extrato %s", caminho)
        raise LeituraExtratoErro("Não foi possível abrir o arquivo enviado.")

    transacoes = _ler_ofx(texto) if extensao == ".ofx" else _ler_csv(texto)
    if not transacoes:
        raise LeituraExtratoErro(
            "Nenhuma transação de saída encontrada no arquivo. Confira se é um extrato "
            "bancário em .ofx ou .csv com valores negativos para as despesas."
        )

    transacoes.sort(key=lambda t: t["data"])
    logger.info("Extrato %s lido: %d saída(s) encontrada(s).", caminho.name, len(transacoes))
    return transacoes


def ler_fatura_cartao(caminho):
    """Lê uma fatura de cartão de crédito em OFX e devolve as compras.

    Bancos divergem no sinal: alguns exportam compras como valores negativos,
    outros como positivos (e pagamentos/estornos com o sinal oposto). A regra:
    se houver valores negativos, eles são as compras; senão, os positivos são.
    Cada item: {'data': 'AAAA-MM-DD', 'descricao': str, 'valor': float > 0}.
    """
    if caminho.suffix.lower() != ".ofx":
        raise LeituraExtratoErro("Envie a fatura do cartão em .ofx (exportada pelo app do banco).")

    try:
        texto = _ler_texto(caminho)
    except OSError:
        logger.exception("Falha ao abrir a fatura %s", caminho)
        raise LeituraExtratoErro("Não foi possível abrir o arquivo enviado.")

    todas = _ler_ofx_bruto(texto)
    negativas = [t for t in todas if t["valor"] < 0]
    compras = negativas if negativas else [t for t in todas if t["valor"] > 0]
    if not compras:
        raise LeituraExtratoErro(
            "Nenhuma compra encontrada no arquivo. Confira se é o OFX da fatura do cartão."
        )

    resultado = [
        {"data": t["data"], "descricao": t["descricao"], "valor": round(abs(t["valor"]), 2)}
        for t in compras
    ]
    resultado.sort(key=lambda t: t["data"])
    logger.info("Fatura %s lida: %d compra(s) encontrada(s).", caminho.name, len(resultado))
    return resultado
