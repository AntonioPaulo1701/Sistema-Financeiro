"""Identidade da clínica — configurável por variável de ambiente.

O código deste repositório é público, então o nome real da clínica não fica
escrito no fonte. Ele vem do ambiente e cai num nome genérico quando ausente.

Para personalizar, defina antes de subir o servidor:

    NOME_CLINICA="Nome da Sua Clínica"
    NOME_TAREFA_AGENDADA="NomeDaSuaClinica_RelatorioDiario"

No Windows, para valer em todas as sessões (inclusive na tarefa agendada):

    setx NOME_CLINICA "Nome da Sua Clínica"
"""

import os

# Aparece nas telas do sistema e nas mensagens enviadas por WhatsApp.
NOME_CLINICA = os.environ.get("NOME_CLINICA", "Clínica Odontológica")

# Nome da tarefa no Agendador de Tarefas do Windows (envio automático diário).
# Atenção: mudar este valor faz o sistema procurar OUTRA tarefa. Se você já tem
# uma tarefa criada com o nome antigo, mantenha o valor ou reagende pela tela
# "Central de Relatórios" e apague a tarefa antiga pelo Agendador.
NOME_TAREFA_AGENDADA = os.environ.get(
    "NOME_TAREFA_AGENDADA", "SistemaFinanceiro_RelatorioDiario"
)
