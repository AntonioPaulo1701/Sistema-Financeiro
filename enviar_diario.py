"""Envio automático do relatório do dia para o WhatsApp.

Este script é executado pelo Agendador de Tarefas do Windows (configurado na
Central de Relatórios). Ele gera o relatório de hoje — mesmo sem nenhuma conta
vencendo — e dispara para o número salvo nas configurações. Na sequência, roda
a Régua de Cobrança Inteligente dos boletos: lembrete prévio (3 dias antes do
vencimento), alerta de "vence hoje" e cobrança dos vencidos, sem repetir
mensagem para quem já foi avisado no mesmo dia. Por fim, dispara os recados
customizados agendados na tela de boletos para a data de hoje (ou datas que
passaram com o computador desligado).

Tudo que acontece fica registrado em logs/envio_automatico.log para conferência.
"""

import logging
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "envio_automatico.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("enviar_diario")


def _enviar_relatorio_do_dia(services, whatsapp):
    """Etapa 1: relatório do dia para os números cadastrados. Devolve True se tudo foi."""
    numeros = [d["numero"] for d in services.listar_destinatarios()]
    if not numeros:
        logger.error(
            "Nenhum número cadastrado para receber o relatório. "
            "Abra a Central de Relatórios e cadastre no painel 'Números do WhatsApp'."
        )
        return False

    hoje = date.today()
    texto = services.gerar_relatorio_texto(hoje)
    logger.info(
        "Relatório de %s gerado. Enviando para %d número(s), um a cada %d segundos...",
        hoje.isoformat(),
        len(numeros),
        whatsapp.INTERVALO_ENTRE_ENVIOS,
    )

    resultados = whatsapp.enviar_para_varios(numeros, texto)
    falharam = [numero for numero, ok, _ in resultados if not ok]
    if falharam:
        logger.error("Envio automático com falhas para: %s", ", ".join(falharam))
        return False

    logger.info("Relatório do dia enviado para todos os %d número(s).", len(numeros))
    return True


def _enviar_regua_cobranca(services, whatsapp, houve_envio_antes):
    """Etapa 2: régua de cobrança dos boletos (lembrete prévio, vence hoje e
    vencidos), sem repetir quem já foi avisado hoje. Devolve True se nada falhou."""
    import time

    if not services.listar_regua_cobranca():
        logger.info("Régua de cobrança: nenhum boleto para avisar hoje.")
        return True

    if houve_envio_antes:
        # Pausa entre o último envio do relatório e a primeira mensagem da régua,
        # no mesmo ritmo usado entre destinatários.
        logger.info("Aguardando %d segundos antes da régua de cobrança...", whatsapp.INTERVALO_ENTRE_ENVIOS)
        time.sleep(whatsapp.INTERVALO_ENTRE_ENVIOS)

    resultados = services.enviar_regua_cobranca()
    falharam = [nome for nome, _, ok, _ in resultados if not ok]
    if falharam:
        logger.error("Régua de cobrança com falhas para: %s", ", ".join(falharam))
        return False
    return True


def _enviar_recados_agendados(services, whatsapp, houve_envio_antes):
    """Etapa 3: recados customizados agendados na tela de boletos para hoje.
    Recado enviado é apagado do boleto; recado que falha fica para a próxima
    varredura. Devolve True se nada falhou."""
    import time

    if not services.listar_mensagens_agendadas():
        logger.info("Recados agendados: nenhum para enviar hoje.")
        return True

    if houve_envio_antes:
        logger.info("Aguardando %d segundos antes dos recados agendados...", whatsapp.INTERVALO_ENTRE_ENVIOS)
        time.sleep(whatsapp.INTERVALO_ENTRE_ENVIOS)

    resultados = services.enviar_mensagens_agendadas()
    falharam = [nome for nome, ok, _ in resultados if not ok]
    if falharam:
        logger.error("Recados agendados com falhas para: %s", ", ".join(falharam))
        return False
    return True


def main():
    import database
    import services
    import whatsapp

    database.init_db()

    # As etapas são independentes: uma falha no relatório não pode impedir a
    # régua de cobrança nem os recados agendados (e vice-versa).
    relatorio_ok = _enviar_relatorio_do_dia(services, whatsapp)
    cobranca_ok = _enviar_regua_cobranca(services, whatsapp, houve_envio_antes=relatorio_ok)
    recados_ok = _enviar_recados_agendados(
        services, whatsapp, houve_envio_antes=relatorio_ok or cobranca_ok
    )

    return 0 if (relatorio_ok and cobranca_ok and recados_ok) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Erro inesperado no envio automático do relatório.")
        sys.exit(1)
