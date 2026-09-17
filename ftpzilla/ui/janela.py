"""A janela principal.

Ordem de empacotamento que vale explicar: a barra de status e empacotada
ANTES do corpo, com side='bottom'. Se ela fosse empacotada por ultimo, numa
tela com DPI alto ou janela baixa ela seria empurrada para fora e o usuario
ficaria sem ver progresso nenhum - o corpo tem expand=True e ganha a disputa.
"""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from tkinter import messagebox, ttk

from .. import __version__, log, util
from ..browser import NavegadorLocal
from ..remotes.local import LocalRemote
from . import tema
from .painel import FilePane

TITULO = "FTPZilla %s" % __version__


class FilaDeLog(logging.Handler):
    """Handler que joga as mensagens numa fila.

    E a unica ponte do log para a interface: a thread que loga nunca toca no
    widget; a thread do Tkinter drena a fila em intervalos.
    """

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait((record.levelno, self.format(record)))
        except Exception:
            pass


class JanelaPrincipal(ttk.Frame):

    def __init__(self, root: tk.Tk, tema_nome: str = tema.PADRAO):
        super().__init__(root)
        self.root = root
        self.tema_nome = tema_nome
        self.logger = log.get()
        self.fila_log: "queue.Queue" = queue.Queue()

        root.title(TITULO)
        root.geometry("1100x700")
        root.minsize(760, 460)
        tema.aplicar(root, tema_nome)

        self.pack(fill="both", expand=True)
        self._montar_menu()
        self._montar_status()      # antes do corpo, de proposito
        self._montar_corpo()
        self._ligar_log()
        self._abrir_paineis()

        root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.after(200, self._drenar_log)

    # ------------------------------------------------------------------
    # Construcao
    # ------------------------------------------------------------------
    def _montar_menu(self) -> None:
        barra = tk.Menu(self.root)

        m_arquivo = tk.Menu(barra, tearoff=0)
        m_arquivo.add_command(label="Nova pasta", accelerator="Ctrl+N",
                              command=lambda: self._foco().nova_pasta())
        m_arquivo.add_command(label="Atualizar", accelerator="F5",
                              command=lambda: self._foco().recarregar())
        m_arquivo.add_separator()
        m_arquivo.add_command(label="Sair", command=self.fechar)
        barra.add_cascade(label="Arquivo", menu=m_arquivo)

        m_exibir = tk.Menu(barra, tearoff=0)
        self.var_tema = tk.StringVar(value=self.tema_nome)
        for nome in tema.THEMES:
            m_exibir.add_radiobutton(label=nome, value=nome,
                                     variable=self.var_tema,
                                     command=self._trocar_tema)
        m_exibir.add_separator()
        m_exibir.add_command(label="Limpar log", command=self._limpar_log)
        barra.add_cascade(label="Exibir", menu=m_exibir)

        m_ajuda = tk.Menu(barra, tearoff=0)
        m_ajuda.add_command(label="Sobre", command=self._sobre)
        barra.add_cascade(label="Ajuda", menu=m_ajuda)

        self.root.configure(menu=barra)
        self.menubar = barra

        self.root.bind_all("<Control-n>", lambda e: self._foco().nova_pasta())
        self.root.bind_all("<F5>", lambda e: self._foco().recarregar())

    def _montar_status(self) -> None:
        barra = ttk.Frame(self, padding=(6, 3))
        barra.pack(side="bottom", fill="x")
        self.lb_status = ttk.Label(barra, text="Pronto.", style="Status.TLabel")
        self.lb_status.pack(side="left", fill="x", expand=True)
        self.pb = ttk.Progressbar(barra, mode="determinate", length=160,
                                  style="Horizontal.TProgressbar")
        self.pb.pack(side="right", padx=(6, 0))
        self.lb_taxa = ttk.Label(barra, text="", style="Status.TLabel")
        self.lb_taxa.pack(side="right")

    def _montar_corpo(self) -> None:
        self.vertical = ttk.PanedWindow(self, orient="vertical")
        self.vertical.pack(fill="both", expand=True)

        self.abas = ttk.Notebook(self.vertical)
        self.vertical.add(self.abas, weight=3)

        self.rodape = ttk.Notebook(self.vertical)
        self.vertical.add(self.rodape, weight=1)

        quadro_log = ttk.Frame(self.rodape)
        self.txt_log = tk.Text(quadro_log, height=8, wrap="none",
                               state="disabled")
        rolar = ttk.Scrollbar(quadro_log, orient="vertical",
                              command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=rolar.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        rolar.pack(side="right", fill="y")
        self.rodape.add(quadro_log, text="Log")

    def _abrir_paineis(self) -> None:
        """Aba inicial: dois paineis locais, lado a lado."""
        aba = ttk.Frame(self.abas)
        self.abas.add(aba, text="Disco local")

        horizontal = ttk.PanedWindow(aba, orient="horizontal")
        horizontal.pack(fill="both", expand=True)

        self.esquerda = FilePane(horizontal, NavegadorLocal(LocalRemote()),
                                 titulo="Local",
                                 ao_transferir=self._transferir,
                                 ao_status=self._status_painel)
        self.direita = FilePane(horizontal, NavegadorLocal(LocalRemote()),
                                titulo="Local",
                                ao_transferir=self._transferir,
                                ao_status=self._status_painel)
        horizontal.add(self.esquerda, weight=1)
        horizontal.add(self.direita, weight=1)

        inicio = self.esquerda.nav.remote.home()
        self.esquerda.ir_para(inicio)
        self.direita.ir_para(inicio)
        self.logger.info("FTPZilla %s iniciado.", __version__)

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------
    def _ligar_log(self) -> None:
        h = FilaDeLog(self.fila_log)
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                         datefmt="%H:%M:%S"))
        self.logger.addHandler(h)
        cores = tema.cores()
        self.txt_log.tag_configure("erro", foreground=cores["tags"]["conflito"])
        self.txt_log.tag_configure("aviso", foreground=cores["tags"]["tamanho_difere"])

    def _drenar_log(self) -> None:
        linhas = []
        try:
            while True:
                linhas.append(self.fila_log.get_nowait())
        except queue.Empty:
            pass
        if linhas:
            self.txt_log.configure(state="normal")
            for nivel, texto in linhas:
                tag = "erro" if nivel >= logging.ERROR else (
                    "aviso" if nivel >= logging.WARNING else "")
                self.txt_log.insert("end", texto + "\n", tag)
            # nao deixa o log crescer sem limite dentro do widget
            if int(self.txt_log.index("end-1c").split(".")[0]) > 2000:
                self.txt_log.delete("1.0", "500.0")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.after(200, self._drenar_log)

    def _limpar_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Acoes
    # ------------------------------------------------------------------
    def _foco(self) -> FilePane:
        """O painel que tem o foco; a esquerda por padrao."""
        w = self.focus_get()
        while w is not None:
            if isinstance(w, FilePane):
                return w
            w = getattr(w, "master", None)
        return self.esquerda

    def _outro(self, painel: FilePane) -> FilePane:
        return self.direita if painel is self.esquerda else self.esquerda

    def _transferir(self, origem: FilePane, itens) -> None:
        # M3 troca isto pela fila; por enquanto o painel so avisa
        destino = self._outro(origem)
        nomes = ", ".join(e.name for e in itens[:3])
        if len(itens) > 3:
            nomes += " (+%d)" % (len(itens) - 3)
        self.status("Transferir %s -> %s (fila chega no M3)"
                    % (nomes, destino.caminho))

    def _status_painel(self, titulo: str, texto: str) -> None:
        self.status(texto)

    def status(self, texto: str) -> None:
        self.lb_status.configure(text=util.elidir(texto, 140))

    def _trocar_tema(self) -> None:
        self.tema_nome = self.var_tema.get()
        tema.aplicar(self.root, self.tema_nome)
        self._ligar_cores_log()
        for p in (self.esquerda, self.direita):
            p.retema()
        tema.pintar_classico(self.root)

    def _ligar_cores_log(self) -> None:
        cores = tema.cores()
        self.txt_log.tag_configure("erro", foreground=cores["tags"]["conflito"])
        self.txt_log.tag_configure("aviso", foreground=cores["tags"]["tamanho_difere"])

    def _sobre(self) -> None:
        messagebox.showinfo(
            "Sobre",
            "FTPZilla %s\n\n"
            "Cliente de transferencia de arquivos.\n"
            "Senhas protegidas pela conta do Windows, fila que sobrevive a\n"
            "fechar o programa e retomada de transferencia interrompida."
            % __version__, parent=self)

    def fechar(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def abrir(tema_nome: str = tema.PADRAO) -> None:
    root = tk.Tk()
    JanelaPrincipal(root, tema_nome)
    root.mainloop()
