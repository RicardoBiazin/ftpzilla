"""Dialogo do "esse arquivo ja existe".

Mostra os dois lados - tamanho e data de cada um - porque e essa comparacao
que a pessoa precisa fazer para decidir. Um dialogo que so diz "o arquivo
existe, substituir?" obriga a adivinhar.

A decisao pode valer para um arquivo so ou para todos de uma vez: ninguem
vai responder trezentas perguntas, e um botao "aplicar a todos" mal feito
faz a pessoa clicar em "substituir tudo" so para se livrar da janela.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Dict, List

from .. import util
from ..conflitos import (PULAR, RENOMEAR, RETOMAR, ROTULOS, SE_MAIS_NOVO,
                         SOBRESCREVER, Conflito)
from . import tema


class ConflitosDialog(tk.Toplevel):
    """Devolve em .decisoes um dicionario indice -> acao. Vazio = cancelou."""

    def __init__(self, master, conflitos: List[Conflito], total: int):
        super().__init__(master)
        self.title("Arquivos que ja existem no destino")
        self.transient(master)
        self.conflitos = conflitos
        self.decisoes: Dict[int, str] = {}
        self.cancelou = True

        corpo = ttk.Frame(self, padding=10)
        corpo.pack(fill="both", expand=True)

        iguais = sum(1 for c in conflitos if c.iguais)
        resumo = ("%d de %d arquivo(s) ja existem na pasta de destino."
                  % (len(conflitos), total))
        if iguais:
            resumo += ("  %d tem o mesmo tamanho e a mesma data - "
                       "provavelmente ja foram baixados." % iguais)
        ttk.Label(corpo, text=resumo, wraplength=760,
                  justify="left").pack(anchor="w", pady=(0, 8))

        quadro = ttk.Frame(corpo)
        quadro.pack(fill="both", expand=True)
        colunas = ("chegando", "existente", "situacao")
        self.tree = ttk.Treeview(quadro, columns=colunas, height=12,
                                 selectmode="extended")
        self.tree.heading("#0", text="Arquivo")
        self.tree.column("#0", width=260)
        for chave, rotulo, largura in (("chegando", "O que esta chegando", 180),
                                       ("existente", "O que ja esta la", 180),
                                       ("situacao", "Situacao", 190)):
            self.tree.heading(chave, text=rotulo)
            self.tree.column(chave, width=largura)
        rolar = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=rolar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        rolar.pack(side="right", fill="y")
        tema.aplicar_tags(self.tree)

        for i, c in enumerate(conflitos):
            self.tree.insert(
                "", "end", iid=str(i), text=c.nome,
                values=("%s  %s" % (util.fmt_bytes(c.tamanho_origem),
                                    util.fmt_data(c.mtime_origem)),
                        "%s  %s" % (util.fmt_bytes(c.tamanho_destino),
                                    util.fmt_data(c.mtime_destino)),
                        _situacao(c)),
                tags=(_cor(c),))

        ttk.Label(corpo, text="O que fazer:").pack(anchor="w", pady=(10, 2))
        self.var_acao = tk.StringVar(value=_sugestao(conflitos))
        for valor, rotulo in ROTULOS:
            estado = "normal"
            if valor == RETOMAR and not any(c.destino_incompleto
                                            for c in conflitos):
                # oferecer "continuar" quando o destino esta completo so
                # levaria a um arquivo truncado
                estado = "disabled"
            ttk.Radiobutton(corpo, text=rotulo, value=valor,
                            variable=self.var_acao,
                            state=estado).pack(anchor="w")

        rodape = ttk.Frame(corpo)
        rodape.pack(fill="x", pady=(12, 0))
        ttk.Button(rodape, text="Aplicar a todos",
                   command=self._todos).pack(side="right")
        ttk.Button(rodape, text="Aplicar so aos selecionados",
                   command=self._selecionados).pack(side="right", padx=6)
        ttk.Button(rodape, text="Cancelar a transferencia",
                   command=self.destroy).pack(side="left")

        tema.pintar_classico(self)
        tema.center_over(self, master)
        self.grab_set()
        master.wait_window(self)

    # ------------------------------------------------------------------
    def _todos(self) -> None:
        acao = self.var_acao.get()
        self.decisoes = {c.indice: acao for c in self.conflitos}
        self.cancelou = False
        self.destroy()

    def _selecionados(self) -> None:
        """Escolhidos levam a acao marcada; o resto fica como esta no
        destino - pular e o padrao seguro para quem nao foi escolhido."""
        acao = self.var_acao.get()
        escolhidos = {int(i) for i in self.tree.selection()}
        if not escolhidos:
            return
        self.decisoes = {c.indice: (acao if i in escolhidos else PULAR)
                         for i, c in enumerate(self.conflitos)}
        self.cancelou = False
        self.destroy()


def _situacao(c: Conflito) -> str:
    if c.iguais:
        return "parece ser o mesmo arquivo"
    if c.destino_incompleto:
        return "o do destino esta incompleto"
    if c.origem_mais_nova:
        return "o que esta chegando e mais novo"
    if c.mtime_destino > c.mtime_origem + 2.0:
        return "o do destino e mais novo"
    return "tamanho diferente"


def _cor(c: Conflito) -> str:
    if c.iguais:
        return "igual"
    if c.destino_incompleto:
        return "tamanho_difere"
    return "conflito"


def _sugestao(conflitos: List[Conflito]) -> str:
    """Comeca marcada a opcao que costuma ser a certa para o caso.

    Nao e adivinhacao automatica: a pessoa continua tendo de confirmar. E
    so evitar que a opcao pre-selecionada seja a mais destrutiva.
    """
    if all(c.iguais for c in conflitos):
        return PULAR
    if any(c.destino_incompleto for c in conflitos):
        return RETOMAR
    return SE_MAIS_NOVO
