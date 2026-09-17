"""A janela principal.

Ordem de empacotamento que vale explicar: a barra de status e empacotada
ANTES do corpo, com side='bottom'. Se ela fosse empacotada por ultimo, numa
tela com DPI alto ou janela baixa ela seria empurrada para fora e o usuario
ficaria sem ver progresso nenhum - o corpo tem expand=True e ganha a disputa.

Diferenca deliberada em relacao ao FileZilla: a aba envolve OS DOIS paineis
(local e servidor), e nao so o remoto. Assim cada servidor guarda tambem a
pasta local em que voce estava trabalhando com ele.
"""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from tkinter import messagebox, ttk

from .. import __version__, log, remotes, segredos, util
from ..sites import GerenteSites, site_rapido
from . import dialogos, tema
from .aba import AbaLocal, AbaSite

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
        self.gerente = GerenteSites().carregar()
        self._vivo = True
        self._abas = []          # [(widget, AbaLocal|AbaSite)]

        root.title(TITULO)
        root.geometry("1180x740")
        root.minsize(780, 480)
        tema.aplicar(root, tema_nome)

        self.pack(fill="both", expand=True)
        self._montar_menu()
        self._montar_status()      # antes do corpo, de proposito
        self._montar_barras()
        self._montar_corpo()
        self._ligar_log()

        self.nova_aba_local()
        self.logger.info("FTPZilla %s iniciado.", __version__)
        aviso = segredos.aviso_se_desprotegido()
        if aviso:
            self.logger.warning("%s", aviso)
            self.status(aviso)

        root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.after(200, self._drenar_log)

    # ------------------------------------------------------------------
    # Construcao
    # ------------------------------------------------------------------
    def _montar_menu(self) -> None:
        barra = tk.Menu(self.root)

        m_arquivo = tk.Menu(barra, tearoff=0)
        m_arquivo.add_command(label="Gerente de sites...", accelerator="Ctrl+S",
                              command=self.abrir_gerente)
        m_arquivo.add_command(label="Nova aba local", accelerator="Ctrl+T",
                              command=self.nova_aba_local)
        m_arquivo.add_command(label="Fechar aba", accelerator="Ctrl+W",
                              command=self.fechar_aba)
        m_arquivo.add_separator()
        m_arquivo.add_command(label="Sair", command=self.fechar)
        barra.add_cascade(label="Arquivo", menu=m_arquivo)

        m_pasta = tk.Menu(barra, tearoff=0)
        m_pasta.add_command(label="Nova pasta", accelerator="Ctrl+N",
                            command=lambda: self._foco().nova_pasta())
        m_pasta.add_command(label="Renomear", accelerator="F2",
                            command=lambda: self._foco().renomear())
        m_pasta.add_command(label="Apagar", accelerator="Del",
                            command=lambda: self._foco().apagar())
        m_pasta.add_separator()
        m_pasta.add_command(label="Atualizar", accelerator="F5",
                            command=lambda: self._foco().recarregar())
        m_pasta.add_command(label="Subir um nivel", accelerator="Backspace",
                            command=lambda: self._foco().subir())
        barra.add_cascade(label="Pasta", menu=m_pasta)

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

        self.root.bind_all("<Control-s>", lambda e: self.abrir_gerente())
        self.root.bind_all("<Control-t>", lambda e: self.nova_aba_local())
        self.root.bind_all("<Control-w>", lambda e: self.fechar_aba())
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

    def _montar_barras(self) -> None:
        """Conexao rapida: para o servidor que se usa uma vez e nao se salva."""
        barra = ttk.Frame(self, padding=(6, 4))
        barra.pack(fill="x")

        ttk.Button(barra, text="Gerente de sites",
                   command=self.abrir_gerente).pack(side="left", padx=(0, 10))

        self.var_tipo = tk.StringVar(value="ftp")
        rotulos = {k: remotes.rotulo(k) for k in remotes.kinds() if k != "local"}
        self._tipo_por_rotulo = {v: k for k, v in rotulos.items()}
        cb = ttk.Combobox(barra, values=sorted(self._tipo_por_rotulo),
                          state="readonly", width=12)
        cb.set(rotulos.get("ftp", "FTP"))
        cb.pack(side="left")
        self.cb_tipo_rapido = cb

        self.var_host = tk.StringVar()
        self.var_usuario = tk.StringVar()
        self.var_senha = tk.StringVar()
        self.var_porta = tk.StringVar()
        for rotulo, var, largura, oculto in (
                ("Servidor:", self.var_host, 22, False),
                ("Usuario:", self.var_usuario, 12, False),
                ("Senha:", self.var_senha, 12, True),
                ("Porta:", self.var_porta, 6, False)):
            ttk.Label(barra, text=rotulo).pack(side="left", padx=(8, 3))
            ent = ttk.Entry(barra, textvariable=var, width=largura,
                            show="•" if oculto else "")
            ent.pack(side="left")
            ent.bind("<Return>", lambda e: self.conexao_rapida())
        ttk.Button(barra, text="Conectar",
                   command=self.conexao_rapida).pack(side="left", padx=8)

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

    # ------------------------------------------------------------------
    # Abas
    # ------------------------------------------------------------------
    def nova_aba_local(self) -> None:
        aba = AbaLocal(self.abas, self)
        self.abas.add(aba, text=aba.titulo)
        self.abas.select(aba)
        self._abas.append(aba)

    def abrir_site(self, site) -> None:
        aba = AbaSite(self.abas, self, site)
        self.abas.add(aba, text=site.nome)
        self.abas.select(aba)
        self._abas.append(aba)

    def atualizar_titulo_aba(self, aba) -> None:
        try:
            self.abas.tab(aba, text=aba.titulo)
        except tk.TclError:
            pass

    def aba_atual(self):
        atual = self.abas.select()
        for aba in self._abas:
            if str(aba) == atual:
                return aba
        return self._abas[0] if self._abas else None

    def fechar_aba(self) -> None:
        aba = self.aba_atual()
        if aba is None:
            return
        if len(self._abas) == 1:
            self.status("A ultima aba nao pode ser fechada.")
            return
        aba.fechar()
        self.abas.forget(aba)
        self._abas.remove(aba)
        aba.destroy()

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------
    def abrir_gerente(self) -> None:
        dialogos.GerenteDialog(self.root, self.gerente,
                               ao_conectar=self.abrir_site)

    def salvar_sites(self) -> None:
        try:
            self.gerente.salvar()
        except OSError as e:
            self.logger.error("Nao foi possivel salvar os sites: %s", e)

    def conexao_rapida(self) -> None:
        host = self.var_host.get().strip()
        if not host:
            self.status("Informe o endereco do servidor.")
            return
        kind = self._tipo_por_rotulo.get(self.cb_tipo_rapido.get(), "ftp")
        try:
            porta = int(self.var_porta.get() or 0)
        except ValueError:
            porta = 0
        site = site_rapido(kind, host, self.var_usuario.get().strip(),
                           self.var_senha.get(), porta)
        # a senha da conexao rapida nao vai para o disco: ela existe so na
        # memoria desta sessao, que e o que "rapida" quer dizer
        self.var_senha.set("")
        self.abrir_site(site)

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------
    def _ligar_log(self) -> None:
        h = FilaDeLog(self.fila_log)
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                         datefmt="%H:%M:%S"))
        self.logger.addHandler(h)
        self._cores_log()

    def _cores_log(self) -> None:
        cores = tema.cores()
        self.txt_log.tag_configure("erro", foreground=cores["tags"]["conflito"])
        self.txt_log.tag_configure("aviso",
                                   foreground=cores["tags"]["tamanho_difere"])

    def _drenar_log(self) -> None:
        if not self._vivo:
            return
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
        # reagenda so enquanto a janela existir: um 'after' pendente numa
        # janela ja destruida vira erro de Tcl no console ao sair
        try:
            self.after(200, self._drenar_log)
        except tk.TclError:
            self._vivo = False

    def _limpar_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Acoes
    # ------------------------------------------------------------------
    def _foco(self):
        """O painel com o foco; o da esquerda da aba atual por padrao."""
        from .painel import FilePane
        w = self.focus_get()
        while w is not None:
            if isinstance(w, FilePane):
                return w
            w = getattr(w, "master", None)
        aba = self.aba_atual()
        return aba.esquerda if aba is not None else None

    def transferir(self, origem, itens) -> None:
        # M3 troca isto pela fila; por enquanto so informa o destino
        aba = self.aba_atual()
        if aba is None:
            return
        destino = (aba.direita if origem is aba.esquerda else aba.esquerda)
        if destino is None:
            self.status("Conecte-se a um servidor para transferir.")
            return
        nomes = ", ".join(e.name for e in itens[:3])
        if len(itens) > 3:
            nomes += " (+%d)" % (len(itens) - 3)
        self.status("Transferir %s para %s (a fila chega no M3)."
                    % (nomes, destino.caminho))

    def status_painel(self, titulo: str, texto: str) -> None:
        self.status(texto)

    def status(self, texto: str) -> None:
        self.lb_status.configure(text=util.elidir(texto, 140))

    def _trocar_tema(self) -> None:
        self.tema_nome = self.var_tema.get()
        tema.aplicar(self.root, self.tema_nome)
        self._cores_log()
        for aba in self._abas:
            aba.retema()
        tema.pintar_classico(self.root)

    def _sobre(self) -> None:
        messagebox.showinfo(
            "Sobre",
            "FTPZilla %s\n\n"
            "Cliente de transferencia de arquivos.\n"
            "Credenciais protegidas pela conta do Windows (%s), fila que\n"
            "sobrevive a fechar o programa e retomada de transferencia\n"
            "interrompida."
            % (__version__, segredos.backend_atual()), parent=self)

    def fechar(self) -> None:
        self._vivo = False
        for aba in self._abas:
            try:
                aba.fechar()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def abrir(tema_nome: str = tema.PADRAO) -> None:
    root = tk.Tk()
    JanelaPrincipal(root, tema_nome)
    root.mainloop()
