"""Janela de busca remota.

Os resultados vao aparecendo conforme a varredura anda. Numa hospedagem com
milhares de pastas a busca leva minutos, e uma tela parada por minutos passa
a impressao de travamento - mesmo estando tudo certo.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import List

from .. import remotes, util
from ..busca import BuscaRemota
from ..fila_store import BAIXAR
from ..remotes.base import Cancelado
from . import tema


class BuscaDialog(tk.Toplevel):

    def __init__(self, master, janela, painel, site):
        super().__init__(master)
        self.title("Procurar no servidor")
        self.transient(master)
        self.janela = janela
        self.painel = painel
        self.site = site
        self.busca = None
        self._achados = []
        self._resposta: "queue.Queue" = queue.Queue()

        corpo = ttk.Frame(self, padding=10)
        corpo.pack(fill="both", expand=True)

        linha = ttk.Frame(corpo)
        linha.pack(fill="x")
        ttk.Label(linha, text="Procurar por:").pack(side="left")
        self.var_padrao = tk.StringVar()
        ent = ttk.Entry(linha, textvariable=self.var_padrao, width=30)
        ent.pack(side="left", padx=6)
        ent.focus_set()
        ent.bind("<Return>", lambda e: self.procurar())
        self.var_so_arquivos = tk.BooleanVar(value=True)
        ttk.Checkbutton(linha, text="so arquivos",
                        variable=self.var_so_arquivos).pack(side="left", padx=6)
        self.bt_procurar = ttk.Button(linha, text="Procurar",
                                      command=self.procurar)
        self.bt_procurar.pack(side="left")
        self.bt_parar = ttk.Button(linha, text="Parar", command=self.parar,
                                   state="disabled")
        self.bt_parar.pack(side="left", padx=4)

        ttk.Label(corpo, style="Fraco.TLabel", justify="left",
                  text=("Procura a partir de %s. Sem * ou ?, procura o texto "
                        "em qualquer parte do nome."
                        % util.elidir(painel.caminho, 60))).pack(
            anchor="w", pady=(6, 4))

        quadro = ttk.Frame(corpo)
        quadro.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(quadro, columns=("pasta", "tamanho", "data"),
                                 selectmode="extended", height=16)
        self.tree.heading("#0", text="Nome")
        self.tree.column("#0", width=220)
        for chave, rotulo, largura, anchor in (("pasta", "Pasta", 320, "w"),
                                               ("tamanho", "Tamanho", 90, "e"),
                                               ("data", "Modificado", 130, "w")):
            self.tree.heading(chave, text=rotulo)
            self.tree.column(chave, width=largura, anchor=anchor)
        rolar = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=rolar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        rolar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.ir_para())

        self.lb_status = ttk.Label(corpo, text="", style="Fraco.TLabel")
        self.lb_status.pack(anchor="w", pady=(6, 0))

        rodape = ttk.Frame(corpo)
        rodape.pack(fill="x", pady=(8, 0))
        ttk.Button(rodape, text="Abrir a pasta",
                   command=self.ir_para).pack(side="left")
        ttk.Button(rodape, text="Baixar selecionados",
                   command=self.baixar).pack(side="left", padx=6)
        ttk.Button(rodape, text="Fechar",
                   command=self.fechar).pack(side="right")

        tema.pintar_classico(self)
        tema.center_over(self, master)
        self.protocol("WM_DELETE_WINDOW", self.fechar)

    # ------------------------------------------------------------------
    def procurar(self) -> None:
        padrao = self.var_padrao.get().strip()
        if not padrao:
            return
        self.tree.delete(*self.tree.get_children())
        self._achados = []
        so_arquivos = self.var_so_arquivos.get()
        raiz = self.painel.caminho
        self.bt_procurar.configure(state="disabled")
        self.bt_parar.configure(state="normal")
        self.lb_status.configure(text="Procurando...")

        def trabalhar():
            remoto = None
            try:
                # conexao propria: a de navegacao nao pode ficar presa numa
                # varredura de minutos
                remoto = remotes.make_remote(self.site.revelado())
                remoto.conectar()
                self.busca = BuscaRemota(remoto, raiz, padrao,
                                         so_arquivos=so_arquivos)
                self.busca.rodar(
                    ao_lote=lambda lote: self._resposta.put(("lote", lote)),
                    ao_pasta=lambda c: self._resposta.put(("pasta", c)))
                self._resposta.put(("fim", self.busca.resumo()))
            except Cancelado:
                self._resposta.put(("fim", "busca interrompida"))
            except BaseException as e:      # noqa: BLE001
                self._resposta.put(("erro", e))
            finally:
                if remoto is not None:
                    try:
                        remoto.fechar()
                    except Exception:
                        pass

        threading.Thread(target=trabalhar, name="busca", daemon=True).start()
        self._drenar()

    def _drenar(self) -> None:
        try:
            while True:
                tipo, valor = self._resposta.get_nowait()
                if tipo == "lote":
                    self._acrescentar(valor)
                elif tipo == "pasta":
                    self.lb_status.configure(
                        text="%d achado(s) - lendo %s"
                        % (len(self._achados), util.elidir(valor, 60)))
                elif tipo == "erro":
                    self.lb_status.configure(text="Falhou: %s" % valor)
                    self._terminou()
                    return
                else:
                    self.lb_status.configure(text=str(valor))
                    self._terminou()
                    return
        except queue.Empty:
            pass
        try:
            self.after(120, self._drenar)
        except tk.TclError:
            pass

    def _acrescentar(self, lote: List) -> None:
        for achado in lote:
            i = len(self._achados)
            self._achados.append(achado)
            e = achado.entrada
            self.tree.insert("", "end", iid="b%d" % i, text=e.name,
                             values=(achado.pasta,
                                     "" if e.is_dir else util.fmt_bytes(e.size),
                                     util.fmt_data(e.mtime)))

    def _terminou(self) -> None:
        self.bt_procurar.configure(state="normal")
        self.bt_parar.configure(state="disabled")

    def parar(self) -> None:
        if self.busca is not None:
            self.busca.parar()

    # ------------------------------------------------------------------
    def _selecionados(self) -> List:
        return [self._achados[int(i[1:])] for i in self.tree.selection()
                if i[1:].isdigit()]

    def ir_para(self) -> None:
        sel = self._selecionados()
        if not sel:
            return
        achado = sel[0]
        destino = (achado.caminho if achado.entrada.is_dir else achado.pasta)
        self.painel.ir_para(destino)
        self.janela.status("Aberto: %s" % destino)

    def baixar(self) -> None:
        sel = [a for a in self._selecionados() if not a.entrada.is_dir]
        if not sel:
            self.lb_status.configure(text="Escolha ao menos um arquivo.")
            return
        aba = self.janela.aba_atual()
        pasta_local = aba.esquerda.caminho
        remoto_local = aba.esquerda.nav.remote
        pares = [(a.caminho, remoto_local.juntar(pasta_local, a.nome),
                  a.entrada.size, a.entrada.mtime) for a in sel]
        self.janela._enfileirar(self.site, BAIXAR, pares)

    def fechar(self) -> None:
        self.parar()
        self.destroy()
