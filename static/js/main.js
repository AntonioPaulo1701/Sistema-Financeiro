// ==========================================================================
// Sistema Financeiro Local - Interações de interface
// Cadastro, status e envio já são reais (Flask + SQLite + WhatsApp). Esta
// camada só cuida de detalhes puramente visuais no navegador.
// ==========================================================================

document.addEventListener("DOMContentLoaded", () => {
  setTopbarDate();
  initCopyReport();
  initDateShortcuts();
  initWhatsappLivePreview();
  initComprovanteUpload();
  initDatePickers();
  initAbasCadastros();
  initTooltips();
  initTemaToggle();
  initModoFocus();
  initPesquisaGlobal();
});

// Barra de Pesquisa Global de Pacientes (topbar, todas as telas). A cada
// tecla (com pausa de 250 ms para não metralhar o servidor) consulta
// /api/pesquisa_pacientes e mostra os resultados num dropdown; clicar num
// paciente abre o prontuário dele. Esc ou clique fora fecham a lista.
function initPesquisaGlobal() {
  const caixa = document.querySelector("[data-pesquisa-global]");
  if (!caixa) return;
  const input = caixa.querySelector("[data-pesquisa-input]");
  const lista = caixa.querySelector("[data-pesquisa-resultados]");
  let timer = null;
  let controlador = null;

  const fechar = () => {
    lista.classList.remove("aberta");
    lista.innerHTML = "";
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const termo = input.value.trim();
    if (termo.length < 2) {
      fechar();
      return;
    }
    timer = setTimeout(async () => {
      if (controlador) controlador.abort(); // descarta a busca anterior ainda em voo
      controlador = new AbortController();
      try {
        const resposta = await fetch(
          `/api/pesquisa_pacientes?q=${encodeURIComponent(termo)}`,
          { signal: controlador.signal }
        );
        const pacientes = await resposta.json();
        lista.innerHTML = "";
        if (!pacientes.length) {
          const vazio = document.createElement("div");
          vazio.className = "pesquisa-vazia";
          vazio.textContent = "Nenhum paciente encontrado";
          lista.appendChild(vazio);
        }
        pacientes.forEach((p) => {
          const item = document.createElement("a");
          item.className = "pesquisa-item";
          item.href = `/pacientes/prontuario/${p.id}`;
          // Miniatura da foto do paciente; sem foto, um círculo com a inicial.
          let foto;
          if (p.tem_foto) {
            foto = document.createElement("img");
            foto.src = `/pacientes/${p.id}/foto`;
            foto.alt = "";
          } else {
            foto = document.createElement("span");
            foto.textContent = (p.nome[0] || "?").toUpperCase();
            foto.classList.add("sem-foto");
          }
          foto.classList.add("pesquisa-foto");
          const nome = document.createElement("strong");
          nome.textContent = p.nome;
          const cpf = document.createElement("span");
          cpf.textContent = p.cpf;
          item.append(foto, nome, cpf);
          lista.appendChild(item);
        });
        lista.classList.add("aberta");
      } catch (erro) {
        // Busca abortada (digitou de novo) ou rede fora: só não mostra nada.
      }
    }, 250);
  });

  input.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape") {
      fechar();
      input.blur();
    }
  });

  document.addEventListener("click", (evento) => {
    if (!caixa.contains(evento.target)) fechar();
  });
}

// Modo Focus (todas as telas). O botão 🧘 da topbar e o flutuante .focus-sair
// alternam a classe .modo-focus-ativo no <body> — o style.css esconde sidebar
// e topbar com transição e o conteúdo ocupa a tela toda. Atalhos: F
// liga/desliga; Esc só desliga. A escolha fica no localStorage (chave
// "modo_focus") e sobrevive a recarregamentos e à troca de tela: o script
// inline do base.html reaplica a classe antes da primeira pintura.
function initModoFocus() {
  const botoes = document.querySelectorAll("[data-focus-toggle]");
  if (!botoes.length) return;

  const alternar = () => {
    const ativo = document.body.classList.toggle("modo-focus-ativo");
    localStorage.setItem("modo_focus", ativo ? "ativo" : "inativo");
  };

  botoes.forEach((botao) => botao.addEventListener("click", alternar));

  document.addEventListener("keydown", (evento) => {
    if (evento.ctrlKey || evento.metaKey || evento.altKey) return;
    // Digitação em campos não pode disparar o atalho (ex: "F" num nome).
    const alvo = evento.target;
    if (alvo.matches("input, textarea, select") || alvo.isContentEditable) return;

    if (evento.key === "f" || evento.key === "F") {
      alternar();
    } else if (evento.key === "Escape" && document.body.classList.contains("modo-focus-ativo")) {
      // Esc com um calendário aberto só fecha o calendário (initDatePickers).
      if (document.querySelector(".dp-wrapper.aberto")) return;
      alternar();
    }
  });
}

// Alternador de tema (claro ↔ Cyberpunk dark). A classe .dark-mode no <body>
// troca as variáveis de cor do style.css; a escolha fica no localStorage e o
// evento "temaAlterado" avisa as telas com gráficos para se repintarem.
function initTemaToggle() {
  const botao = document.querySelector("[data-tema-toggle]");
  if (!botao) return;

  const atualizarIcone = () => {
    const escuro = document.body.classList.contains("dark-mode");
    botao.textContent = escuro ? "☀️" : "🌙";
    botao.title = escuro ? "Voltar para o tema claro" : "Ativar o modo escuro holográfico";
  };
  atualizarIcone(); // o tema salvo já foi aplicado pelo script inline do base.html

  botao.addEventListener("click", () => {
    const escuro = document.body.classList.toggle("dark-mode");
    localStorage.setItem("tema", escuro ? "dark" : "light");
    atualizarIcone();
    document.dispatchEvent(new CustomEvent("temaAlterado"));
  });
}

// Tooltip estilizado para elementos com data-tooltip (ex: selo de risco dos
// boletos). Um único balão fixo acompanha o mouse por cima de qualquer tabela,
// sem ser cortado pelo overflow do .table-wrapper.
function initTooltips() {
  const balao = document.createElement("div");
  balao.className = "tooltip-balao";
  document.body.appendChild(balao);

  document.addEventListener("mouseover", (evento) => {
    const alvo = evento.target.closest("[data-tooltip]");
    if (!alvo) {
      balao.classList.remove("visivel");
      return;
    }
    balao.textContent = alvo.dataset.tooltip;
    balao.classList.add("visivel");

    const area = alvo.getBoundingClientRect();
    // Centraliza acima do elemento, sem sair da janela.
    let esquerda = area.left + area.width / 2 - balao.offsetWidth / 2;
    esquerda = Math.max(8, Math.min(esquerda, window.innerWidth - balao.offsetWidth - 8));
    balao.style.left = `${esquerda}px`;
    balao.style.top = `${area.top - balao.offsetHeight - 8}px`;
  });
}

// Abas da tela de Cadastros (Plano de Contas / Categorias / Fornecedores).
function initAbasCadastros() {
  const botoes = document.querySelectorAll(".aba-botao");
  if (!botoes.length) return;

  botoes.forEach((botao) =>
    botao.addEventListener("click", () => {
      botoes.forEach((b) => b.classList.toggle("ativa", b === botao));
      document.querySelectorAll("[data-aba-conteudo]").forEach((painel) => {
        painel.classList.toggle("ativa", painel.dataset.abaConteudo === botao.dataset.aba);
      });
    })
  );
}

// ==========================================================================
// Calendário próprio do sistema (substitui o seletor padrão do navegador)
// Troca todo input[type=date] e input[type=month] por um campo estilizado
// que abre um calendário flutuante com a cara do sistema.
// ==========================================================================

const DP_MESES = [
  "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
  "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
];
const DP_MESES_CURTOS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];
const DP_DIAS_SEMANA = ["D", "S", "T", "Q", "Q", "S", "S"];
const DP_ICONE =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
  '<rect x="3" y="4" width="18" height="17" rx="3"></rect><path d="M8 2v4M16 2v4M3 10h18"></path>' +
  '<path d="M9 15.5l2 2 4-4.5"></path></svg>';

function initDatePickers() {
  document.querySelectorAll('input[type="date"]').forEach((input) => criarDatePicker(input, "data"));
  document.querySelectorAll('input[type="month"]').forEach((input) => criarDatePicker(input, "mes"));

  // Fecha qualquer calendário aberto ao clicar fora dele. Elementos que foram
  // recém-substituídos (ex: setas de trocar mês, que o re-render remove do DOM)
  // são ignorados — senão o calendário fecharia ao navegar entre meses.
  document.addEventListener("click", (evento) => {
    if (!document.body.contains(evento.target)) return;
    document.querySelectorAll(".dp-wrapper.aberto").forEach((wrapper) => {
      if (!wrapper.contains(evento.target)) wrapper._dpFechar();
    });
  });
  document.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape") {
      document.querySelectorAll(".dp-wrapper.aberto").forEach((wrapper) => wrapper._dpFechar());
    }
  });
}

function criarDatePicker(input, tipo) {
  const wrapper = document.createElement("div");
  wrapper.className = "dp-wrapper";
  input.parentNode.insertBefore(wrapper, input);
  wrapper.appendChild(input);

  const obrigatorio = input.required;
  input.required = false;
  input.type = "hidden"; // o valor ISO continua sendo enviado pelo mesmo name

  const campo = document.createElement("button");
  campo.type = "button";
  campo.className = "dp-campo";
  campo.innerHTML = `<span class="dp-campo-texto"></span><span class="dp-campo-icone">${DP_ICONE}</span>`;
  wrapper.appendChild(campo);

  const pop = document.createElement("div");
  pop.className = "dp-pop";
  wrapper.appendChild(pop);

  const hoje = new Date();
  let dataVista = lerValor() || new Date(hoje.getFullYear(), hoje.getMonth(), 1);

  function lerValor() {
    if (!input.value) return null;
    const [ano, mes, dia] = input.value.split("-").map(Number);
    return new Date(ano, mes - 1, dia || 1);
  }

  function paraISO(data) {
    const iso = `${data.getFullYear()}-${String(data.getMonth() + 1).padStart(2, "0")}`;
    return tipo === "mes" ? iso : `${iso}-${String(data.getDate()).padStart(2, "0")}`;
  }

  function atualizarCampo() {
    const texto = campo.querySelector(".dp-campo-texto");
    const valor = lerValor();
    if (!valor) {
      texto.textContent = tipo === "mes" ? "Escolher mês" : "Escolher data";
      campo.classList.remove("preenchido");
    } else if (tipo === "mes") {
      texto.textContent = `${DP_MESES[valor.getMonth()]} de ${valor.getFullYear()}`;
      campo.classList.add("preenchido");
    } else {
      texto.textContent = valor.toLocaleDateString("pt-BR");
      campo.classList.add("preenchido");
    }
  }

  function selecionar(data) {
    input.value = paraISO(data);
    atualizarCampo();
    campo.classList.remove("dp-anima-pop");
    void campo.offsetWidth; // reinicia a animação
    campo.classList.add("dp-anima-pop");
    fechar();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function render() {
    tipo === "mes" ? renderMeses() : renderDias();
  }

  // Campos de nascimento precisam alcançar décadas passadas no seletor de ano;
  // os demais (vencimentos, relatórios) ficam com a janela curta de sempre.
  const anosParaTras = (input.name || "").includes("nascimento") ? 100 : 6;

  function opcoesDeAno(anoSelecionado) {
    const anoAtual = new Date().getFullYear();
    const inicio = Math.min(anoAtual - anosParaTras, anoSelecionado - 3);
    const fim = Math.max(anoAtual + 10, anoSelecionado + 3);
    let opcoes = "";
    for (let ano = inicio; ano <= fim; ano++) {
      opcoes += `<option value="${ano}" ${ano === anoSelecionado ? "selected" : ""}>${ano}</option>`;
    }
    return opcoes;
  }

  function renderDias() {
    const ano = dataVista.getFullYear();
    const mes = dataVista.getMonth();
    const selecionado = lerValor();
    const primeiroDiaSemana = new Date(ano, mes, 1).getDay();
    const diasNoMes = new Date(ano, mes + 1, 0).getDate();
    const diasMesAnterior = new Date(ano, mes, 0).getDate();

    let celulas = "";
    for (let i = primeiroDiaSemana - 1; i >= 0; i--) {
      celulas += `<span class="dp-dia fora">${diasMesAnterior - i}</span>`;
    }
    for (let dia = 1; dia <= diasNoMes; dia++) {
      const data = new Date(ano, mes, dia);
      const classes = ["dp-dia"];
      if (data.toDateString() === new Date().toDateString()) classes.push("hoje");
      if (selecionado && data.toDateString() === selecionado.toDateString()) classes.push("selecionado");
      celulas += `<button type="button" class="${classes.join(" ")}" data-dia="${dia}">${dia}</button>`;
    }
    const restante = (7 - ((primeiroDiaSemana + diasNoMes) % 7)) % 7;
    for (let dia = 1; dia <= restante; dia++) {
      celulas += `<span class="dp-dia fora">${dia}</span>`;
    }

    const opcoesMes = DP_MESES.map(
      (nome, indice) => `<option value="${indice}" ${indice === mes ? "selected" : ""}>${nome}</option>`
    ).join("");

    pop.innerHTML = `
      <div class="dp-cabecalho">
        <button type="button" class="dp-nav" data-nav="-1" title="Mês anterior">‹</button>
        <div class="dp-seletores">
          <select class="dp-select dp-select-mes" data-sel-mes title="Escolher o mês">${opcoesMes}</select>
          <select class="dp-select dp-select-ano" data-sel-ano title="Escolher o ano">${opcoesDeAno(ano)}</select>
        </div>
        <button type="button" class="dp-nav" data-nav="1" title="Próximo mês">›</button>
      </div>
      <div class="dp-semana">${DP_DIAS_SEMANA.map((d) => `<span>${d}</span>`).join("")}</div>
      <div class="dp-grade-dias">${celulas}</div>
      <div class="dp-rodape">
        <button type="button" class="dp-hoje">Hoje</button>
      </div>`;

    pop.querySelectorAll("[data-nav]").forEach((btn) =>
      btn.addEventListener("click", () => {
        dataVista = new Date(ano, mes + parseInt(btn.dataset.nav, 10), 1);
        render();
      })
    );
    const selMes = pop.querySelector("[data-sel-mes]");
    const selAno = pop.querySelector("[data-sel-ano]");
    const aoTrocarSeletor = () => {
      dataVista = new Date(parseInt(selAno.value, 10), parseInt(selMes.value, 10), 1);
      render();
    };
    selMes.addEventListener("change", aoTrocarSeletor);
    selAno.addEventListener("change", aoTrocarSeletor);
    pop.querySelectorAll("[data-dia]").forEach((btn) =>
      btn.addEventListener("click", () => selecionar(new Date(ano, mes, parseInt(btn.dataset.dia, 10))))
    );
    pop.querySelector(".dp-hoje").addEventListener("click", () => {
      const agora = new Date();
      dataVista = new Date(agora.getFullYear(), agora.getMonth(), 1);
      selecionar(agora);
    });
  }

  function renderMeses() {
    const ano = dataVista.getFullYear();
    const selecionado = lerValor();

    const celulas = DP_MESES_CURTOS.map((nome, indice) => {
      const classes = ["dp-mes"];
      if (selecionado && selecionado.getFullYear() === ano && selecionado.getMonth() === indice) {
        classes.push("selecionado");
      }
      if (new Date().getFullYear() === ano && new Date().getMonth() === indice) classes.push("hoje");
      return `<button type="button" class="${classes.join(" ")}" data-mes="${indice}">${nome}</button>`;
    }).join("");

    pop.innerHTML = `
      <div class="dp-cabecalho">
        <button type="button" class="dp-nav" data-ano="-1" title="Ano anterior">‹</button>
        <select class="dp-select dp-select-ano" data-sel-ano title="Escolher o ano">${opcoesDeAno(ano)}</select>
        <button type="button" class="dp-nav" data-ano="1" title="Próximo ano">›</button>
      </div>
      <div class="dp-grade-meses">${celulas}</div>`;

    pop.querySelectorAll("[data-ano]").forEach((btn) =>
      btn.addEventListener("click", () => {
        dataVista = new Date(ano + parseInt(btn.dataset.ano, 10), dataVista.getMonth(), 1);
        render();
      })
    );
    pop.querySelector("[data-sel-ano]").addEventListener("change", (evento) => {
      dataVista = new Date(parseInt(evento.target.value, 10), dataVista.getMonth(), 1);
      render();
    });
    pop.querySelectorAll("[data-mes]").forEach((btn) =>
      btn.addEventListener("click", () => selecionar(new Date(ano, parseInt(btn.dataset.mes, 10), 1)))
    );
  }

  function abrir() {
    document.querySelectorAll(".dp-wrapper.aberto").forEach((outro) => {
      if (outro !== wrapper) outro._dpFechar();
    });
    dataVista = lerValor() || new Date(new Date().getFullYear(), new Date().getMonth(), 1);
    render();
    wrapper.classList.add("aberto");
    // Abre para cima quando não há espaço embaixo.
    const rect = pop.getBoundingClientRect();
    pop.classList.toggle("acima", rect.bottom > window.innerHeight - 12);
  }

  function fechar() {
    wrapper.classList.remove("aberto");
    pop.classList.remove("acima");
  }

  wrapper._dpFechar = fechar;
  campo.addEventListener("click", () => (wrapper.classList.contains("aberto") ? fechar() : abrir()));

  // Campo obrigatório vazio: bloqueia o envio, abre o calendário e "treme".
  if (obrigatorio && input.form) {
    input.form.addEventListener("submit", (evento) => {
      if (!input.value) {
        evento.preventDefault();
        abrir();
        campo.classList.remove("dp-anima-shake");
        void campo.offsetWidth;
        campo.classList.add("dp-anima-shake");
      }
    });
  }

  atualizarCampo();
}

// Botão de clipe na tabela de contas: abre o seletor de arquivo e envia o
// comprovante assim que o usuário escolhe (sem precisar de outro clique).
function initComprovanteUpload() {
  document.querySelectorAll("[data-comprovante-form]").forEach((form) => {
    const botao = form.querySelector("[data-comprovante-btn]");
    const input = form.querySelector("[data-comprovante-input]");
    if (!botao || !input) return;

    botao.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      if (input.files.length > 0) form.submit();
    });
  });
}

// Atalhos de período na Central de Relatórios. O número do atalho indica os
// dias: negativo olha para trás (últimos N dias até hoje), positivo para
// frente (de hoje até N dias), zero é só o dia de hoje.
function initDateShortcuts() {
  const form = document.querySelector("[data-periodo-form]");
  if (!form) return;

  const inputInicio = document.getElementById("inicio-relatorio");
  const inputFim = document.getElementById("fim-relatorio");
  const inputMes = document.querySelector("[data-mes-relatorio]");

  const paraISO = (data) => data.toLocaleDateString("sv-SE"); // YYYY-MM-DD local

  document.querySelectorAll("[data-periodo-atalho]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dias = parseInt(btn.dataset.periodoAtalho, 10);
      const hoje = new Date();
      const alvo = new Date();
      alvo.setDate(alvo.getDate() + dias);

      inputInicio.value = paraISO(dias < 0 ? alvo : hoje);
      inputFim.value = paraISO(dias < 0 ? hoje : alvo);
      form.submit();
    });
  });

  // Escolher um mês (ex: 2026-05) gera o relatório do mês inteiro.
  if (inputMes) {
    inputMes.addEventListener("change", () => {
      if (!inputMes.value) return;
      window.location.href = `${form.action}?mes=${inputMes.value}`;
    });
  }
}

// Editar o texto do relatório atualiza a bolha de pré-visualização na hora.
function initWhatsappLivePreview() {
  const textarea = document.querySelector("[data-report-preview]");
  const bubble = document.querySelector("[data-whatsapp-preview]");
  if (!textarea || !bubble) return;

  textarea.addEventListener("input", () => {
    bubble.textContent = textarea.value;
  });
}

function setTopbarDate() {
  const el = document.querySelector("[data-topbar-date]");
  if (!el) return;
  const hoje = new Date();
  el.textContent = hoje.toLocaleDateString("pt-BR", {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric",
  });
}

// Botão "Copiar Texto" na Central de Relatórios.
function initCopyReport() {
  const copyBtn = document.querySelector("[data-copy-report]");
  const preview = document.querySelector("[data-report-preview]");
  if (!copyBtn || !preview) return;

  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(preview.value);
      flashButton(copyBtn, "Copiado!");
    } catch (err) {
      flashButton(copyBtn, "Não foi possível copiar");
    }
  });
}

function flashButton(button, tempLabel) {
  const original = button.textContent;
  button.textContent = tempLabel;
  button.disabled = true;
  setTimeout(() => {
    button.textContent = original;
    button.disabled = false;
  }, 1500);
}
