"""Agendamento do envio automático do relatório diário via WhatsApp.

Usa o Agendador de Tarefas nativo do Windows (schtasks): custo zero e roda
mesmo com o sistema financeiro fechado — basta o computador estar ligado e o
usuário logado no horário escolhido.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path

import config

logger = logging.getLogger(__name__)

NOME_TAREFA = config.NOME_TAREFA_AGENDADA
SCRIPT_ENVIO = Path(__file__).resolve().parent / "enviar_diario.py"
PADRAO_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class AutomacaoErro(Exception):
    """Falha ao criar/remover a tarefa agendada no Windows."""


def _rodar_schtasks(argumentos):
    try:
        return subprocess.run(
            ["schtasks", *argumentos],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as erro:
        logger.exception("Falha ao executar schtasks %s", argumentos)
        raise AutomacaoErro("Não foi possível falar com o Agendador de Tarefas do Windows.") from erro


def tarefa_ativa():
    return _rodar_schtasks(["/Query", "/TN", NOME_TAREFA]).returncode == 0


def ativar_envio_diario(hora):
    """Cria (ou atualiza) a tarefa diária que roda enviar_diario.py no horário dado."""
    if not PADRAO_HORA.match(hora or ""):
        raise AutomacaoErro("Informe um horário válido no formato HH:MM (ex: 08:00).")

    comando = f'"{sys.executable}" "{SCRIPT_ENVIO}"'
    resultado = _rodar_schtasks(
        ["/Create", "/TN", NOME_TAREFA, "/TR", comando, "/SC", "DAILY", "/ST", hora, "/F"]
    )
    if resultado.returncode != 0:
        logger.error("schtasks /Create falhou: %s", resultado.stderr.strip() or resultado.stdout.strip())
        raise AutomacaoErro("O Windows recusou a criação da tarefa agendada. Detalhe no log do terminal.")
    logger.info("Tarefa '%s' agendada todos os dias às %s.", NOME_TAREFA, hora)


def desativar_envio_diario():
    if not tarefa_ativa():
        return
    resultado = _rodar_schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])
    if resultado.returncode != 0:
        logger.error("schtasks /Delete falhou: %s", resultado.stderr.strip() or resultado.stdout.strip())
        raise AutomacaoErro("Não foi possível remover a tarefa agendada.")
    logger.info("Tarefa '%s' removida.", NOME_TAREFA)
