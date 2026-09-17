"""A fila na tela.

Regra de desempenho que molda este arquivo: a cada 250 ms so sao atualizadas
as linhas dos itens EM ANDAMENTO - tipicamente menos de dez. Redesenhar a
fila inteira nesse ritmo seria O(n) sobre cinco mil linhas quatro vezes por
segundo, e a janela pararia de responder justamente quando ha trabalho.

A lista completa so e reconstruida quando a fila muda de verdade (item novo,
item que terminou, reordenacao), e ainda assim com um atraso curto para
agrupar varias mudancas seguidas numa unica redesenhada.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List

from .. import util
from ..fila_store import (CANCELADO, CONCLUIDO, ENVIAR, ESPERANDO, FALHOU,
                          PAUSADO, PULADO, RODANDO)
from . import tema

COLUNAS = (
    ("sentido", "", 28, "center"),
    ("nome", "Arquivo", 200, "w"),
    ("servidor", "Servidor", 110, "w"),
    ("tamanho", "Tamanho", 90, "e"),
    ("progresso", "Progresso", 90, "e"),
    ("velocidade", "Velocidade", 90, "e"),
    ("restante", "Restante", 70, "e"),
    ("estado", "Estado", 90, "w"),
    ("detalhe", "Observacao", 260, "w"),
)

#: como cada estado e pintado (chaves existentes em tema.THEMES[...]["tags"])
COR_ESTADO = {
    RODANDO: "esq_mais_nova",
    CONCLUIDO: "so_esquerda",
    FALHOU: "conflito",
    PAUSADO: "tamanho_difere",
    CANCELADO: "igual",
    PULADO: "igual",
}

ROTULO_ESTADO = {
    ESPERANDO: "Na fila",
    RODANDO: "Transferindo",
    PAUSADO: "Pausado",
    CONCLUIDO: "Concluido",
    FALHOU: "Falhou",
    CANCELADO: "Cancelado",
    PULADO: "Pulado",
}


class FilaView(ttk.Frame):

    def __init__(self, master, gerenciador, ao_status=None):
        super().__init__(master, padding=(4, 4))
        self.fila = gerenciador
        self.ao_status = ao_status
        self._por_iid: Dict[str, int] = {}     # iid -> id do item
        self._linhas: Dict[int, str] = {}      # id do item -> iid
        self._sujo = False
        self._pendente = None

        self._montar()
        self.after(250, self._tick)

    # ------------------------------------------------------------------
    def _montar(self) -> None:
        barra = ttk.Frame(self)
        barra.pack(fill="x", pady=(0, 3))
        for texto, comando in (
                ("Pausar", self.pausar),
                ("Retomar", self.retomar),
                ("Tentar de novo", self.tentar_de_novo),
                ("Cancelar", self.cancelar),
                ("Remover", self.remover),
                ("↑", lambda: self.mover(True)),
                ("↓", lambda: self.mover(False)),
                ("Limpar concluidos", self.limpar)):
            largura = 3 if texto in ("↑", "↓") else None
            b = ttk.Button(barra, text=texto, command=comando)
            if largura:
                b.configure(width=largura)
            b.pack(side="left", padx=2)

        self.lb_resumo = ttk.Label(barra, text="", style="Fraco.TLabel")
        self.lb_resumo.pack(side="right", padx=6)

        quadro = ttk.Frame(self)
        quadro.pack(fill="both", expand=True)
        colunas = tuple(c[0] for c in COLUNAS)
        self.tree = ttk.Treeview(quadro, columns=colunas, show="headings",
                                 selectmode="extended")
        for chave, rotulo, largura, anchor in COLUNAS:
            self.tree.heading(chave, text=rotulo)
            self.tree.column(chave, width=largura, minwidth=24, anchor=anchor,
                             stretch=(chave == "detalhe"))
        rolar = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=rolar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        rolar.pack(side="right", fill="y")
        tema.aplicar_tags(self.tree)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Pausar", command=self.pausar)
        self.menu.add_command(label="Retomar", command=self.retomar)
        self.menu.add_command(label="Tentar de novo", command=self.tentar_de_novo)
        self.menu.add_separator()
        self.menu.add_command(label="Cancelar", command=self.cancelar)
        self.menu.add_command(label="Remover da fila", command=self.remover)
        self.tree.bind("<Button-3>", self._menu_contexto)
        self.tree.bind("<Delete>", lambda e: self.remover())

    # ------------------------------------------------------------------
    # Desenho
    # ------------------------------------------------------------------
    def marcar_sujo(self) -> None:
        """Chamado de OUTRA thread (a da fila): so levanta a bandeira."""
        self._sujo = True

    def _tick(self) -> None:
        try:
            if self._sujo:
                self._sujo = False
                self.redesenhar()
            else:
                self._atualizar_em_curso()
            self._resumo()
        except tk.TclError:
            return           # janela fechando
        self.after(250, self._tick)

    def redesenhar(self) -> None:
        selecao = set(self.selecionados())
        self.tree.delete(*self.tree.get_children())
        self._por_iid.clear()
        self._linhas.clear()
        for item in list(self.fila.itens):
            iid = "f%d" % item.id
            self.tree.insert("", "end", iid=iid, values=self._valores(item),
                             tags=self._tags(item))
            self._por_iid[iid] = item.id
            self._linhas[item.id] = iid
            if item.id in selecao:
                self.tree.selection_add(iid)

    def _atualizar_em_curso(self) -> None:
        for item in self.fila.por_estado(RODANDO):
            iid = self._linhas.get(item.id)
            if iid is None:
                self._sujo = True
                return
            try:
                self.tree.item(iid, values=self._valores(item),
                               tags=self._tags(item))
            except tk.TclError:
                self._sujo = True
                return

    @staticmethod
    def _tags(item) -> tuple:
        cor = COR_ESTADO.get(item.estado)
        return (cor,) if cor else ()

    @staticmethod
    def _valores(item) -> tuple:
        s = item.snapshot()
        progresso = "%.0f%%" % s["pct"] if s["total"] else (
            util.fmt_bytes(s["feitos"]) if s["feitos"] else "")
        detalhe = item.erro
        if not detalhe:
            detalhe = item.destino if item.sentido == ENVIAR else item.destino
        return (
            "↑" if item.sentido == ENVIAR else "↓",
            item.nome,
            item.site_nome,
            util.fmt_bytes(item.tamanho) if item.tamanho >= 0 else "-",
            progresso,
            util.fmt_velocidade(s["velocidade"]) if item.estado == RODANDO else "",
            util.fmt_tempo(s["eta"]) if item.estado == RODANDO else "",
            ROTULO_ESTADO.get(item.estado, item.estado),
            util.elidir(detalhe, 80),
        )

    def _resumo(self) -> None:
        r = self.fila.resumo()
        contagem = r.get("contagem", {})
        partes = []
        if contagem.get(RODANDO):
            partes.append("%d transferindo" % contagem[RODANDO])
        if contagem.get(ESPERANDO):
            partes.append("%d na fila" % contagem[ESPERANDO])
        if contagem.get(FALHOU):
            partes.append("%d com falha" % contagem[FALHOU])
        if contagem.get(CONCLUIDO):
            partes.append("%d concluido(s)" % contagem[CONCLUIDO])
        if r.get("velocidade"):
            partes.append(util.fmt_velocidade(r["velocidade"]))
        if r.get("pausada"):
            partes.append("FILA PAUSADA")
        self.lb_resumo.configure(text="   ".join(partes) or "Fila vazia.")

    # ------------------------------------------------------------------
    # Acoes
    # ------------------------------------------------------------------
    def selecionados(self) -> List[int]:
        return [self._por_iid[i] for i in self.tree.selection()
                if i in self._por_iid]

    def _alvos(self) -> List[int]:
        ids = self.selecionados()
        if not ids and self.ao_status is not None:
            self.ao_status("Escolha ao menos um item da fila.")
        return ids

    def pausar(self) -> None:
        self.fila.pausar(self._alvos())
        self.marcar_sujo()

    def retomar(self) -> None:
        self.fila.retomar(self._alvos())
        self.marcar_sujo()

    def tentar_de_novo(self) -> None:
        self.fila.tentar_de_novo(self.selecionados() or None)
        self.marcar_sujo()

    def cancelar(self) -> None:
        ids = self._alvos()
        if ids:
            self.fila.cancelar(ids)
            self.marcar_sujo()

    def remover(self) -> None:
        ids = self._alvos()
        if ids:
            self.fila.remover(ids)
            self.marcar_sujo()

    def mover(self, para_cima: bool) -> None:
        ids = self._alvos()
        if ids:
            self.fila.mover(ids, para_cima)
            self.marcar_sujo()

    def limpar(self) -> None:
        n = self.fila.limpar_encerrados()
        self.marcar_sujo()
        if self.ao_status is not None:
            self.ao_status("%d item(ns) saiu(ram) da fila." % n)

    def _menu_contexto(self, evento) -> None:
        iid = self.tree.identify_row(evento.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        try:
            self.menu.tk_popup(evento.x_root, evento.y_root)
        finally:
            self.menu.grab_release()

    def retema(self) -> None:
        tema.aplicar_tags(self.tree)
        tema.pintar_classico(self)
        self.redesenhar()
