import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

PADRAO_NUMERO = re.compile(r"^\+\d{10,15}$")

# Pausa entre um destinatário e o próximo quando o relatório vai para vários números.
INTERVALO_ENTRE_ENVIOS = 120  # segundos (2 minutos)


class EnvioWhatsAppInvalido(Exception):
    """Erro ao validar os dados ou disparar o envio pelo WhatsApp."""


def validar_numero(numero):
    numero = (numero or "").strip().replace(" ", "").replace("-", "")
    if not PADRAO_NUMERO.match(numero):
        raise EnvioWhatsAppInvalido(
            "Informe o número no formato internacional, com código do país e DDD. Ex: +5511999999999"
        )
    return numero


def enviar_mensagem(numero, mensagem):
    """Abre o WhatsApp Web no navegador padrão e envia a mensagem automaticamente.

    Usa a biblioteca gratuita pywhatkit, que depende do WhatsApp Web já estar
    autenticado (QR code escaneado) no navegador padrão desta máquina local.
    """
    numero_validado = validar_numero(numero)
    if not (mensagem or "").strip():
        raise EnvioWhatsAppInvalido("O texto do relatório está vazio.")

    try:
        import pywhatkit
    except ImportError:
        logger.exception("Biblioteca pywhatkit não encontrada")
        raise EnvioWhatsAppInvalido(
            "Dependência 'pywhatkit' não instalada. Rode: pip install -r requirements.txt"
        )

    try:
        logger.info("Enviando relatório para %s via WhatsApp Web...", numero_validado)
        pywhatkit.sendwhatmsg_instantly(
            phone_no=numero_validado,
            message=mensagem,
            wait_time=15,
            tab_close=True,
            close_time=3,
        )
        logger.info("Mensagem enviada para %s.", numero_validado)
    except EnvioWhatsAppInvalido:
        raise
    except Exception:
        logger.exception("Falha ao enviar mensagem para %s via WhatsApp", numero_validado)
        raise EnvioWhatsAppInvalido(
            "Não foi possível enviar a mensagem. Verifique se o navegador abriu o WhatsApp Web "
            "corretamente e se você está logado (QR code escaneado)."
        )


def enviar_para_varios(numeros, mensagem, intervalo=INTERVALO_ENTRE_ENVIOS):
    """Envia a mesma mensagem para vários números, um de cada vez, com pausa entre eles.

    Devolve uma lista de (numero, sucesso, erro) — um envio que falhar não
    impede os próximos.
    """
    resultados = []
    for indice, numero in enumerate(numeros):
        if indice > 0:
            logger.info("Aguardando %d segundos antes do próximo envio...", intervalo)
            time.sleep(intervalo)
        try:
            enviar_mensagem(numero, mensagem)
            resultados.append((numero, True, None))
        except EnvioWhatsAppInvalido as erro:
            logger.error("Envio para %s falhou: %s", numero, erro)
            resultados.append((numero, False, str(erro)))
    sucessos = sum(1 for _, ok, _ in resultados if ok)
    logger.info("Envio em lote finalizado: %d de %d mensagens enviadas.", sucessos, len(numeros))
    return resultados


def enviar_para_varios_em_segundo_plano(numeros, mensagem, intervalo=INTERVALO_ENTRE_ENVIOS):
    """Dispara o envio em lote numa thread para não travar a tela do sistema.

    Com a pausa de 2 minutos entre números, 3 destinatários levam ~4 minutos —
    a interface responde na hora e os envios seguem pelo navegador.
    """
    thread = threading.Thread(
        target=enviar_para_varios,
        args=(list(numeros), mensagem, intervalo),
        daemon=True,
        name="envio-whatsapp-lote",
    )
    thread.start()
    logger.info("Envio em lote iniciado em segundo plano para %d número(s).", len(numeros))
