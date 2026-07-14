"""Leitura do PDF da fatura de cartão de crédito.

Extrai os valores monetários (formato brasileiro) do texto do PDF para que a
conciliação com as contas cadastradas seja automática, sem digitação manual.
"""

import logging
import re

import pdfplumber

logger = logging.getLogger(__name__)

# Valores no padrão brasileiro: 1.234,56 ou 234,56. O lookbehind/lookahead
# evita capturar pedaços de números maiores (ex: "1234,567").
PADRAO_VALOR = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?!\d)")

# Palavras que costumam acompanhar o valor total nas faturas dos bancos.
PALAVRAS_TOTAL = (
    "total da fatura",
    "total desta fatura",
    "total a pagar",
    "valor total",
    "valor da fatura",
    "total fatura",
)


class LeituraFaturaErro(Exception):
    """Falha ao abrir ou interpretar o PDF da fatura."""


def _converter_valor(texto):
    return float(texto.replace(".", "").replace(",", "."))


def extrair_dados_fatura(caminho_pdf):
    """Lê o PDF e devolve (valores, total_detectado).

    - valores: lista de todos os valores monetários encontrados no texto.
    - total_detectado: valor na linha que menciona "total" (ou None se não achar).
    Um PDF digitalizado (imagem, sem texto) devolve lista vazia.
    """
    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            texto = "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)
    except Exception as erro:
        logger.exception("Falha ao abrir o PDF da fatura em %s", caminho_pdf)
        raise LeituraFaturaErro("Não foi possível abrir o PDF enviado.") from erro

    valores = [_converter_valor(m.group()) for m in PADRAO_VALOR.finditer(texto)]

    total_detectado = None
    for linha in texto.splitlines():
        linha_baixa = linha.lower()
        if any(palavra in linha_baixa for palavra in PALAVRAS_TOTAL):
            achados = PADRAO_VALOR.findall(linha)
            if achados:
                # Se a linha tiver mais de um valor, o total costuma ser o maior.
                total_detectado = max(_converter_valor(v) for v in achados)
                break

    logger.info(
        "PDF %s lido: %d valor(es) encontrados, total detectado: %s",
        caminho_pdf,
        len(valores),
        total_detectado,
    )
    return valores, total_detectado
