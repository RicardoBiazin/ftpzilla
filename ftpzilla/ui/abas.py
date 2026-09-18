"""Notebook com botao de fechar em cada aba.

O ttk.Notebook nao tem isso pronto. O jeito certo - e nao um X de mentira
colado no texto - e registrar um ELEMENTO de imagem no tema e reescrever o
layout da aba para incluir esse elemento ao lado do rotulo. Ai o X ganha
area propria, o clique pode ser identificado por identify() e o ttk cuida
do estado (normal, mouse em cima, pressionado).

Duas armadilhas resolvidas aqui:

1. O elemento e criado no interpretador Tcl, nao no widget. Criar duas
   vezes com o mesmo nome levanta TclError, e criar em um interpretador
   novo (outra janela, ou os testes) e obrigatorio. Por isso o registro e
   controlado por interpretador, do mesmo jeito que os icones.
2. Imagem de ttk sem referencia viva em Python some. As PhotoImage ficam
   num dicionario de modulo de proposito.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .. import log

logger = log.get()

ELEMENTO = "FTPZilla.fechar"
ESTILO = "Fechavel.TNotebook"

_IMAGENS = {}        # mantem as PhotoImage vivas
_REGISTRADO = [None]  # em qual interpretador Tk o elemento existe


def _x(cor: str, fundo: str = "", tam: int = 14) -> tk.PhotoImage:
    """Desenha um X de tam x tam. Fundo vazio = transparente."""
    img = tk.PhotoImage(width=tam, height=tam)
    if fundo:
        img.put(fundo, to=(0, 0, tam, tam))
    margem = 4
    grossura = 2
    for i in range(margem, tam - margem):
        for d in range(grossura):
            for x, y in ((i, i + d), (i + d, i),
                         (i, tam - 1 - i - d), (i + d, tam - 1 - i)):
                if 0 <= x < tam and 0 <= y < tam:
                    img.put(cor, to=(x, y, x + 1, y + 1))
    return img


def _registrar(widget) -> bool:
    """Cria o elemento e o layout. Uma vez por interpretador Tk."""
    raiz = widget.winfo_toplevel()
    interpretador = getattr(tk, "_default_root", None)
    if _REGISTRADO[0] is interpretador:
        return True

    estilo = ttk.Style(raiz)
    try:
        estilo.theme_use("clam")
    except tk.TclError:
        pass

    # cinza neutro de proposito: legivel tanto no tema claro quanto no
    # escuro, sem precisar recriar o elemento a cada troca de tema (o que
    # o Tcl nao deixa fazer com o mesmo nome)
    _IMAGENS.clear()
    _IMAGENS["normal"] = _x("#8a8a8a")
    _IMAGENS["sobre"] = _x("#ffffff", "#c0392b")
    _IMAGENS["pressionado"] = _x("#ffffff", "#96281b")

    try:
        estilo.element_create(
            ELEMENTO, "image", _IMAGENS["normal"],
            ("active", "pressed", "!disabled", _IMAGENS["pressionado"]),
            ("active", "!disabled", _IMAGENS["sobre"]),
            border=6, sticky="")
    except tk.TclError as e:
        # ja existia neste interpretador: seguir com o layout mesmo assim
        logger.debug("elemento de fechar aba ja registrado: %s", e)

    # ATENCAO: dentro de um layout se referenciam os ELEMENTOS do tema
    # ("Notebook.tab", "Notebook.label"...), e nao o nome do estilo. Escrever
    # "Fechavel.TNotebook.tab" nao da erro: o ttk simplesmente desenha nada,
    # e o X some sem nenhuma mensagem.
    estilo.layout(ESTILO, [("Notebook.client", {"sticky": "nswe"})])
    estilo.layout("%s.Tab" % ESTILO, [
        ("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe",
                                  "children": [
                ("Notebook.focus", {"side": "top", "sticky": "nswe",
                                    "children": [
                    ("Notebook.label", {"side": "left", "sticky": ""}),
                    (ELEMENTO, {"side": "left", "sticky": ""}),
                ]}),
            ]}),
        ]}),
    ])
    _REGISTRADO[0] = interpretador
    return True


class NotebookFechavel(ttk.Notebook):
    """Notebook em que cada aba tem um X.

    Tambem fecha com o botao do meio, que e o gesto que quem usa navegador
    tenta primeiro, e oferece um menu de contexto na propria aba.
    """

    def __init__(self, master, ao_fechar: Optional[Callable] = None,
                 ao_menu: Optional[Callable] = None, **kwargs):
        _registrar(master)
        kwargs.setdefault("style", ESTILO)
        super().__init__(master, **kwargs)
        self.ao_fechar = ao_fechar
        self.ao_menu = ao_menu
        self._pressionada = None

        self.bind("<ButtonPress-1>", self._pressionou, add="+")
        self.bind("<ButtonRelease-1>", self._soltou, add="+")
        self.bind("<ButtonRelease-2>", self._botao_do_meio, add="+")
        self.bind("<ButtonPress-3>", self._contexto, add="+")

    # ------------------------------------------------------------------
    def _no_x(self, evento) -> bool:
        try:
            return ELEMENTO in self.identify(evento.x, evento.y)
        except tk.TclError:
            return False

    def _aba_em(self, evento):
        try:
            return self.index("@%d,%d" % (evento.x, evento.y))
        except tk.TclError:
            return None

    def _pressionou(self, evento):
        if not self._no_x(evento):
            return None
        self._pressionada = self._aba_em(evento)
        # 'pressed' faz o X escurecer enquanto o botao esta apertado
        self.state(["pressed"])
        return "break"

    def _soltou(self, evento):
        if "pressed" not in self.state():
            return None
        self.state(["!pressed"])
        alvo = self._aba_em(evento)
        # so fecha se soltar sobre a MESMA aba em que apertou - arrastar
        # para fora e a forma padrao de desistir de um clique
        if alvo is not None and alvo == self._pressionada and self._no_x(evento):
            self._fechar(alvo)
        self._pressionada = None
        return "break"

    def _botao_do_meio(self, evento):
        aba = self._aba_em(evento)
        if aba is not None:
            self._fechar(aba)
            return "break"
        return None

    def _contexto(self, evento):
        aba = self._aba_em(evento)
        if aba is None:
            return None
        if self.ao_menu is not None:
            self.ao_menu(aba, evento)
        return "break"

    def _fechar(self, indice: int) -> None:
        if self.ao_fechar is None:
            return
        try:
            widget = self.nametowidget(self.tabs()[indice])
        except (IndexError, KeyError, tk.TclError):
            return
        self.ao_fechar(widget)
