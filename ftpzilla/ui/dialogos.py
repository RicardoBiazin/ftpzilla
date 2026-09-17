"""Dialogos: gerente de sites, senha, certificado e chave de host.

O formulario de conexao NAO e escrito a mao. Ele e gerado a partir do
registro de tipos (remotes.campos), entao um protocolo novo aparece aqui
completo, com rotulo e ajuda, sem ninguem editar este arquivo. Foi o que
manteve o gerente de sites do mesmo tamanho com dois protocolos e com sete.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from .. import remotes, segredos
from ..sites import GerenteSites, Site
from . import tema


class FormularioSite(ttk.Frame):
    """Os campos de conexao de um site, montados a partir do registro."""

    def __init__(self, master, ao_mudar=None):
        super().__init__(master, padding=(8, 6))
        self.ao_mudar = ao_mudar
        self.site: Optional[Site] = None
        self._vars: Dict[str, tk.Variable] = {}
        self._widgets: List[tk.Widget] = []

        cabecalho = ttk.Frame(self)
        cabecalho.pack(fill="x")
        ttk.Label(cabecalho, text="Nome:").grid(row=0, column=0, sticky="w",
                                                pady=2)
        self.var_nome = tk.StringVar()
        ttk.Entry(cabecalho, textvariable=self.var_nome, width=28).grid(
            row=0, column=1, sticky="we", pady=2)

        ttk.Label(cabecalho, text="Grupo:").grid(row=1, column=0, sticky="w",
                                                 pady=2)
        self.var_grupo = tk.StringVar()
        ttk.Entry(cabecalho, textvariable=self.var_grupo, width=28).grid(
            row=1, column=1, sticky="we", pady=2)

        ttk.Label(cabecalho, text="Tipo:").grid(row=2, column=0, sticky="w",
                                                pady=2)
        self._rotulos = {k: remotes.rotulo(k) for k in remotes.kinds()
                         if k != "local"}
        self._por_rotulo = {v: k for k, v in self._rotulos.items()}
        self.var_tipo = tk.StringVar()
        self.cb_tipo = ttk.Combobox(cabecalho, textvariable=self.var_tipo,
                                    values=sorted(self._por_rotulo),
                                    state="readonly", width=26)
        self.cb_tipo.grid(row=2, column=1, sticky="we", pady=2)
        self.cb_tipo.bind("<<ComboboxSelected>>", lambda e: self._trocar_tipo())
        cabecalho.columnconfigure(1, weight=1)

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=(8, 6))

        self.quadro = ttk.Frame(self)
        self.quadro.pack(fill="both", expand=True)
        self.quadro.columnconfigure(1, weight=1)

        self.lb_nota = ttk.Label(self, text="", style="Fraco.TLabel",
                                 wraplength=380, justify="left")
        self.lb_nota.pack(fill="x", pady=(6, 0))
        self.lb_aviso = ttk.Label(self, text="", style="Erro.TLabel",
                                  wraplength=380, justify="left")
        self.lb_aviso.pack(fill="x")

    # ------------------------------------------------------------------
    def carregar(self, site: Optional[Site]) -> None:
        self.site = site
        if site is None:
            self._limpar_quadro()
            self.var_nome.set("")
            self.var_grupo.set("")
            self.var_tipo.set("")
            return
        self.var_nome.set(site.nome)
        self.var_grupo.set(site.grupo)
        self.var_tipo.set(self._rotulos.get(site.kind, site.kind))
        self._montar_campos(site.kind)

    def _trocar_tipo(self) -> None:
        if self.site is None:
            return
        self.aplicar()      # guarda o que ja foi digitado
        self.site.kind = self._por_rotulo.get(self.var_tipo.get(), self.site.kind)
        self._montar_campos(self.site.kind)
        if self.ao_mudar is not None:
            self.ao_mudar()

    def _limpar_quadro(self) -> None:
        for w in self.quadro.winfo_children():
            w.destroy()
        self._vars.clear()
        self._widgets = []

    def _montar_campos(self, kind: str) -> None:
        self._limpar_quadro()
        site = self.site
        linha = 0
        for campo in remotes.campos(kind):
            valor = site.get(campo.key, campo.default)
            if campo.secreto:
                # o valor protegido nunca e mostrado; a caixa fica com um
                # marcador e so grava se o usuario digitar algo novo
                valor = "•" * 8 if segredos.protegido(valor or "") else (valor or "")

            ttk.Label(self.quadro, text=campo.label + ":").grid(
                row=linha, column=0, sticky="w", pady=2, padx=(0, 6))
            w = self._widget(campo, valor)
            w.grid(row=linha, column=1, sticky="we", pady=2)
            linha += 1
            if campo.help:
                ttk.Label(self.quadro, text=campo.help, style="Fraco.TLabel",
                          wraplength=320, justify="left").grid(
                    row=linha, column=1, sticky="w", pady=(0, 4))
                linha += 1

        spec = remotes.get_spec(kind) if kind in remotes.kinds() else None
        self.lb_nota.configure(text=(spec.note if spec else ""))

        faltando = remotes.missing_requirements(kind) if spec else []
        avisos = []
        if faltando:
            avisos.append("Este tipo precisa de: %s (pip install %s)"
                          % (", ".join(faltando), " ".join(faltando)))
        aviso_segredo = segredos.aviso_se_desprotegido()
        if aviso_segredo:
            avisos.append(aviso_segredo)
        self.lb_aviso.configure(text="\n".join(avisos))

    def _widget(self, campo, valor):
        if campo.kind == "bool":
            var = tk.BooleanVar(value=bool(valor))
            w = ttk.Checkbutton(self.quadro, variable=var, text="")
        elif campo.kind == "int":
            var = tk.StringVar(value=str(valor or campo.default or 0))
            w = ttk.Entry(self.quadro, textvariable=var, width=10)
        elif campo.kind == "password":
            var = tk.StringVar(value=str(valor or ""))
            w = ttk.Entry(self.quadro, textvariable=var, show="•",
                          width=campo.width)
        elif campo.kind == "escolha":
            rotulos = {v: r for v, r in campo.opcoes}
            var = tk.StringVar(value=rotulos.get(valor, str(valor or "")))
            w = ttk.Combobox(self.quadro, textvariable=var,
                             values=[r for _, r in campo.opcoes],
                             state="readonly", width=campo.width)
            w._valor_por_rotulo = {r: v for v, r in campo.opcoes}
        elif campo.kind in ("file", "dir"):
            var = tk.StringVar(value=str(valor or ""))
            w = ttk.Frame(self.quadro)
            ttk.Entry(w, textvariable=var, width=campo.width).pack(
                side="left", fill="x", expand=True)
            ttk.Button(w, text="...", width=3,
                       command=lambda v=var, k=campo.kind: self._escolher(v, k)
                       ).pack(side="left", padx=(3, 0))
        else:
            var = tk.StringVar(value=str(valor or ""))
            w = ttk.Entry(self.quadro, textvariable=var, width=campo.width)
        self._vars[campo.key] = var
        self._widgets.append(w)
        return w

    def _escolher(self, var: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            caminho = filedialog.askdirectory(parent=self)
        else:
            caminho = filedialog.askopenfilename(parent=self)
        if caminho:
            var.set(caminho)

    def aplicar(self) -> Optional[Site]:
        """Joga o que esta na tela de volta no Site."""
        site = self.site
        if site is None:
            return None
        site.nome = self.var_nome.get().strip() or site.nome
        site.grupo = self.var_grupo.get().strip()
        for campo in remotes.campos(site.kind):
            if campo.key not in self._vars:
                continue
            valor = self._vars[campo.key].get()
            if campo.kind == "escolha":
                for w in self._widgets:
                    mapa = getattr(w, "_valor_por_rotulo", None)
                    if mapa and valor in mapa:
                        valor = mapa[valor]
                        break
            if campo.secreto:
                # marcador intocado = manter o que ja estava gravado
                if valor == "•" * 8:
                    continue
            site.set(campo.key, valor)
        return site


class GerenteDialog(tk.Toplevel):
    """Lista de sites a esquerda, formulario a direita."""

    def __init__(self, master, gerente: GerenteSites, ao_conectar=None):
        super().__init__(master)
        self.title("Gerente de sites")
        self.transient(master)
        self.gerente = gerente
        self.ao_conectar = ao_conectar
        self.resultado: Optional[Site] = None

        corpo = ttk.Frame(self, padding=8)
        corpo.pack(fill="both", expand=True)

        esquerda = ttk.Frame(corpo)
        esquerda.pack(side="left", fill="both", expand=False)
        self.tree = ttk.Treeview(esquerda, show="tree", selectmode="browse",
                                 height=16)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selecionou())
        self.tree.bind("<Double-1>", lambda e: self.conectar())

        botoes = ttk.Frame(esquerda)
        botoes.pack(fill="x", pady=(6, 0))
        ttk.Button(botoes, text="Novo", command=self.novo).pack(side="left")
        ttk.Button(botoes, text="Duplicar", command=self.duplicar).pack(
            side="left", padx=3)
        ttk.Button(botoes, text="Apagar", command=self.apagar).pack(side="left")

        ttk.Separator(corpo, orient="vertical").pack(side="left", fill="y",
                                                     padx=8)

        direita = ttk.Frame(corpo)
        direita.pack(side="left", fill="both", expand=True)
        self.form = FormularioSite(direita)
        self.form.pack(fill="both", expand=True)

        rodape = ttk.Frame(self, padding=(8, 0, 8, 8))
        rodape.pack(fill="x")
        ttk.Button(rodape, text="Conectar", command=self.conectar).pack(
            side="right")
        ttk.Button(rodape, text="Salvar", command=self.salvar).pack(
            side="right", padx=6)
        ttk.Button(rodape, text="Fechar", command=self.destroy).pack(
            side="right")

        self._recarregar_lista()
        tema.pintar_classico(self)
        tema.center_over(self, master)
        self.grab_set()

    # ------------------------------------------------------------------
    def _recarregar_lista(self, selecionar: str = "") -> None:
        self.tree.delete(*self.tree.get_children())
        grupos = {}
        for site in self.gerente.ordenados():
            pai = ""
            if site.grupo:
                if site.grupo not in grupos:
                    grupos[site.grupo] = self.tree.insert(
                        "", "end", text=site.grupo, open=True)
                pai = grupos[site.grupo]
            self.tree.insert(pai, "end", iid=site.id, text=site.nome)
        if selecionar and self.tree.exists(selecionar):
            self.tree.selection_set(selecionar)
            self.tree.see(selecionar)

    def _site_selecionado(self) -> Optional[Site]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.gerente.por_id(sel[0])

    def _selecionou(self) -> None:
        self.form.carregar(self._site_selecionado())

    # ------------------------------------------------------------------
    def novo(self) -> None:
        site = Site(nome="Novo site", kind="ftp")
        self.gerente.adicionar(site)
        self._recarregar_lista(site.id)
        self.form.carregar(site)

    def duplicar(self) -> None:
        site = self._site_selecionado()
        if site is None:
            return
        novo = self.gerente.adicionar(site.copia())
        self.gerente.salvar()
        self._recarregar_lista(novo.id)

    def apagar(self) -> None:
        site = self._site_selecionado()
        if site is None:
            return
        if not messagebox.askyesno("Apagar site",
                                   "Apagar \"%s\"?" % site.nome, parent=self):
            return
        self.gerente.remover(site.id)
        self.gerente.salvar()
        self.form.carregar(None)
        self._recarregar_lista()

    def salvar(self) -> Optional[Site]:
        site = self.form.aplicar()
        if site is not None:
            self.gerente.salvar()
            self._recarregar_lista(site.id)
        return site

    def conectar(self) -> None:
        site = self.salvar()
        if site is None:
            messagebox.showinfo("Gerente de sites",
                                "Escolha um site na lista.", parent=self)
            return
        if not site.host:
            messagebox.showwarning("Gerente de sites",
                                   "Informe o endereco do servidor.",
                                   parent=self)
            return
        self.resultado = site
        self.destroy()
        if self.ao_conectar is not None:
            self.ao_conectar(site)


# ---------------------------------------------------------------------------
# Dialogos de confianca
# ---------------------------------------------------------------------------
def pedir_senha(master, titulo: str, mensagem: str) -> Optional[str]:
    """Pede a senha sem repeti-la automaticamente depois.

    Senha errada repetida tranca conta em servidor com politica de bloqueio,
    entao a fila para e espera esta janela em vez de tentar de novo sozinha.
    """
    janela = tk.Toplevel(master)
    janela.title(titulo)
    janela.transient(master)
    resposta = {"valor": None}

    quadro = ttk.Frame(janela, padding=12)
    quadro.pack(fill="both", expand=True)
    ttk.Label(quadro, text=mensagem, wraplength=340,
              justify="left").pack(anchor="w", pady=(0, 8))
    var = tk.StringVar()
    ent = ttk.Entry(quadro, textvariable=var, show="•", width=32)
    ent.pack(fill="x")
    ent.focus_set()

    def confirmar():
        resposta["valor"] = var.get()
        janela.destroy()

    botoes = ttk.Frame(quadro)
    botoes.pack(fill="x", pady=(10, 0))
    ttk.Button(botoes, text="OK", command=confirmar).pack(side="right")
    ttk.Button(botoes, text="Cancelar",
               command=janela.destroy).pack(side="right", padx=6)
    ent.bind("<Return>", lambda e: confirmar())
    ent.bind("<Escape>", lambda e: janela.destroy())

    tema.pintar_classico(janela)
    tema.center_over(janela, master)
    janela.grab_set()
    master.wait_window(janela)
    return resposta["valor"]


def confirmar_certificado(master, host: str, fingerprint: str,
                          detalhe: str = "") -> bool:
    """Certificado que nao valida. Aceitar FIXA aquele certificado exato."""
    texto = ("O certificado de %s nao pode ser validado.\n\n"
             "%s\n\n"
             "Impressao digital SHA-256:\n%s\n\n"
             "Confira a impressao com o administrador do servidor. Ao aceitar,"
             " o FTPZilla passa a exigir EXATAMENTE este certificado nas"
             " proximas conexoes - e avisa se ele mudar."
             % (host, detalhe or "Certificado autoassinado ou de uma "
                "autoridade desconhecida.", fingerprint))
    return messagebox.askyesno("Certificado nao confiavel", texto,
                               icon="warning", parent=master)


def confirmar_chave_host(master, host: str, fingerprint: str,
                         mudou: bool) -> str:
    """Chave de host SSH. Devolve 'salvar', 'uma_vez' ou 'nao'.

    Chave MUDADA nao ganha botao de salvar em um clique: ou e troca legitima
    de servidor, ou e alguem no meio do caminho, e o usuario precisa parar
    para descobrir qual dos dois.
    """
    if mudou:
        messagebox.showerror(
            "A chave do servidor MUDOU",
            "A chave de %s nao e a mesma de antes.\n\n"
            "Nova impressao SHA-256:\n%s\n\n"
            "Isso acontece quando o servidor e reinstalado - e tambem quando "
            "alguem se coloca no meio da conexao. A conexao foi recusada.\n\n"
            "Se a troca foi legitima, remova a linha antiga do arquivo "
            "known_hosts do FTPZilla." % (host, fingerprint),
            parent=master)
        return "nao"

    janela = tk.Toplevel(master)
    janela.title("Servidor desconhecido")
    janela.transient(master)
    escolha = {"valor": "nao"}

    quadro = ttk.Frame(janela, padding=12)
    quadro.pack(fill="both", expand=True)
    ttk.Label(quadro, wraplength=400, justify="left",
              text=("Primeira conexao com %s.\n\nImpressao digital SHA-256 da "
                    "chave do servidor:" % host)).pack(anchor="w")
    ttk.Label(quadro, text=fingerprint, style="Accent.TLabel").pack(
        anchor="w", pady=6)
    ttk.Label(quadro, wraplength=400, justify="left", style="Fraco.TLabel",
              text=("Compare com a impressao que o administrador informou. "
                    "Conferir agora e a unica chance de detectar um servidor "
                    "impostor.")).pack(anchor="w")

    def responder(valor):
        escolha["valor"] = valor
        janela.destroy()

    botoes = ttk.Frame(quadro)
    botoes.pack(fill="x", pady=(12, 0))
    ttk.Button(botoes, text="Confio: salvar",
               command=lambda: responder("salvar")).pack(side="right")
    ttk.Button(botoes, text="So desta vez",
               command=lambda: responder("uma_vez")).pack(side="right", padx=6)
    ttk.Button(botoes, text="Cancelar",
               command=lambda: responder("nao")).pack(side="right")

    tema.pintar_classico(janela)
    tema.center_over(janela, master)
    janela.grab_set()
    master.wait_window(janela)
    return escolha["valor"]
