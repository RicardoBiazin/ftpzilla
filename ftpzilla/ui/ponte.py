"""A ponte das threads de trabalho para a thread do Tkinter.

Chamar widget.after() de dentro de outra thread e o atalho que todo mundo
usa e que funciona quase sempre - ate nao funcionar. O Tcl nao e reentrante:
dependendo do momento, a chamada e ignorada, ou derruba a thread em
silencio, e o sintoma e uma operacao que simplesmente nunca termina, sem
erro nenhum no log. Foi exatamente isso que aconteceu aqui com a comparacao
de pastas.

A solucao e a mesma que ja se usa para o log e para a fila: a thread de
trabalho so poe a funcao numa fila comum de Python, e a thread do Tkinter
drena essa fila num 'after' periodico, que e o unico lugar do programa
autorizado a tocar em widget.
"""
from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

from .. import log

logger = log.get()

INTERVALO = 50      # ms entre drenagens


class Ponte:
    """Fila de callbacks para rodar na thread do Tkinter.

    E chamavel com (atraso, funcao) para poder ser usada onde se esperava um
    'root.after' - inclusive pelo BrowserWorker, que nao conhece Tkinter.
    """

    def __init__(self, widget, intervalo: int = INTERVALO):
        self.widget = widget
        self.intervalo = intervalo
        self.fila: "queue.Queue[Callable]" = queue.Queue()
        self._vivo = True
        self._agendar_drenagem()

    def __call__(self, atraso, funcao) -> None:
        """Assinatura de root.after, mas segura fora da thread principal."""
        if not self._vivo:
            return
        self.fila.put(funcao)

    def chamar(self, funcao) -> None:
        self.fila.put(funcao)

    def _agendar_drenagem(self) -> None:
        try:
            self.widget.after(self.intervalo, self._drenar)
        except tk.TclError:
            self._vivo = False

    def _drenar(self) -> None:
        if not self._vivo:
            return
        # limite por rodada: uma enxurrada de callbacks nao pode segurar a
        # interface por segundos sem redesenhar
        for _ in range(200):
            try:
                funcao = self.fila.get_nowait()
            except queue.Empty:
                break
            try:
                funcao()
            except tk.TclError:
                return              # a janela fechou no meio
            except Exception:       # noqa: BLE001
                logger.exception("Erro num callback vindo de outra thread")
        self._agendar_drenagem()

    def parar(self) -> None:
        self._vivo = False
