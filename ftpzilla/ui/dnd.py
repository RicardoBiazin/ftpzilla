"""Arrastar e soltar, com o pouco que o Tkinter permite.

Tres casos, e vale ser franco sobre cada um:

1. DENTRO da janela (painel -> painel, painel -> fila): feito em Tkinter
   puro, com um Toplevel sem borda seguindo o mouse e winfo_containing()
   para descobrir onde a pessoa soltou. Cobre a maior parte do uso e nao
   custa dependencia nenhuma.

2. DO EXPLORER PARA A JANELA: o Tkinter nao fala com o gerenciador de
   arquivos do sistema. Precisa do pacote opcional tkinterdnd2 (que embrulha
   o tkdnd). Sem ele, o programa roda igual e so este caminho some.

3. DA JANELA PARA O EXPLORER: nao e viavel. Exigiria implementar as
   interfaces COM IDataObject/IDropSource e o formato CFSTR_FILEDESCRIPTOR
   para arquivos que ainda nem existem no disco. Em vez de fingir, o menu de
   contexto oferece "Baixar para..." e "Copiar caminho" - e o README diz
   isso com todas as letras.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, List, Optional

from .. import log

logger = log.get()


def criar_raiz() -> tk.Tk:
    """tk.Tk com suporte a soltar arquivos do sistema, quando disponivel.

    Precisa ser decidido ANTES de qualquer widget existir: o tkinterdnd2
    exige que a propria raiz seja a classe dele.
    """
    try:
        from tkinterdnd2 import TkinterDnD
        raiz = TkinterDnD.Tk()
        raiz._ftpzilla_dnd = True
        return raiz
    except Exception:
        raiz = tk.Tk()
        raiz._ftpzilla_dnd = False
        return raiz


def disponivel(widget) -> bool:
    try:
        return bool(getattr(widget.winfo_toplevel(), "_ftpzilla_dnd", False))
    except Exception:
        return False


def registrar_alvo(widget, ao_soltar: Callable[[List[str]], None]) -> bool:
    """Faz o widget aceitar arquivos soltos do Explorer."""
    if not disponivel(widget):
        return False
    try:
        from tkinterdnd2 import DND_FILES
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>",
                        lambda e: ao_soltar(_separar(e.data)))
        return True
    except Exception as e:      # noqa: BLE001
        logger.debug("Nao deu para registrar o alvo de arrastar: %s", e)
        return False


def _separar(dados: str) -> List[str]:
    """O tkdnd entrega os caminhos numa lista do Tcl: com chaves quando ha
    espaco no nome. Separar no espaco quebraria 'C:/Meus Documentos'."""
    caminhos, atual, dentro = [], "", False
    for c in dados or "":
        if c == "{":
            dentro = True
        elif c == "}":
            dentro = False
            caminhos.append(atual)
            atual = ""
        elif c == " " and not dentro:
            if atual:
                caminhos.append(atual)
            atual = ""
        else:
            atual += c
    if atual:
        caminhos.append(atual)
    return [c for c in caminhos if c]


class ArrastoInterno:
    """Arrastar entre widgets da propria janela, em Tkinter puro."""

    def __init__(self, origem, obter_itens: Callable, ao_soltar: Callable,
                 rotulo: Callable = None):
        self.origem = origem
        self.obter_itens = obter_itens
        self.ao_soltar = ao_soltar
        self.rotulo = rotulo or (lambda itens: "%d item(ns)" % len(itens))
        self._fantasma: Optional[tk.Toplevel] = None
        self._itens = None
        self._inicio = None

        origem.bind("<ButtonPress-1>", self._pressionou, add="+")
        origem.bind("<B1-Motion>", self._arrastou, add="+")
        origem.bind("<ButtonRelease-1>", self._soltou, add="+")

    def _pressionou(self, evento) -> None:
        self._inicio = (evento.x, evento.y)
        self._itens = None

    def _arrastou(self, evento) -> None:
        if self._inicio is None:
            return
        # so comeca a arrastar depois de alguns pixels: senao um clique com
        # a mao tremida viraria um arrasto
        if self._fantasma is None:
            if abs(evento.x - self._inicio[0]) < 6 and \
                    abs(evento.y - self._inicio[1]) < 6:
                return
            itens = self.obter_itens()
            if not itens:
                return
            self._itens = itens
            self._fantasma = self._criar_fantasma(self.rotulo(itens))
        self._fantasma.geometry("+%d+%d" % (evento.x_root + 12,
                                            evento.y_root + 12))

    def _criar_fantasma(self, texto: str) -> tk.Toplevel:
        from . import tema
        cores = tema.cores()
        janela = tk.Toplevel(self.origem)
        janela.overrideredirect(True)
        janela.attributes("-topmost", True)
        try:
            janela.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        tk.Label(janela, text=texto, bg=cores["field"], fg=cores["fg"],
                 padx=8, pady=3).pack()
        return janela

    def _soltou(self, evento) -> None:
        self._inicio = None
        if self._fantasma is None:
            return
        self._fantasma.destroy()
        self._fantasma = None
        alvo = self.origem.winfo_containing(evento.x_root, evento.y_root)
        if alvo is not None and self._itens:
            self.ao_soltar(alvo, self._itens)
        self._itens = None
