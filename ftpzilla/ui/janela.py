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
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import __version__, log, remotes, segredos, util
from ..fila import GerenciadorFila
from ..fila_store import BAIXAR, ENVIAR
from ..sites import GerenteSites, site_rapido
from . import dialogos, tema
from .ponte import Ponte
from .aba import AbaLocal, AbaSite
from ..editor import EditorRemoto
from . import dnd
from .busca_view import BuscaDialog
from .fila_view import FilaView
from .sincronizar import SincronizarDialog, comparar_nos_paineis

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
        # unica via de volta das threads de trabalho para a interface
        self.ponte = Ponte(self)
        self.editor = EditorRemoto(ao_modificar=self._editor_salvou,
                                   ao_status=self._status_de_thread)
        self._montar_menu()
        self._montar_status()      # antes do corpo, de proposito
        self._montar_barras()
        self._montar_corpo()
        self._ligar_log()
        self._abrir_fila()

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

        m_fila = tk.Menu(barra, tearoff=0)
        m_fila.add_command(label="Pausar a fila",
                           command=lambda: self.fila.pausar_tudo())
        m_fila.add_command(label="Retomar a fila",
                           command=lambda: self.fila.retomar_tudo())
        m_fila.add_command(label="Tentar de novo os que falharam",
                           command=lambda: self.fila.tentar_de_novo())
        m_fila.add_separator()
        m_fila.add_command(label="Comparar esta pasta", accelerator="Ctrl+D",
                           command=self.comparar_pastas)
        m_fila.add_command(label="Sincronizar pastas...",
                           command=self.sincronizar)
        m_fila.add_command(label="Procurar no servidor...",
                           accelerator="Ctrl+F", command=self.procurar)
        m_fila.add_separator()
        m_limite = tk.Menu(m_fila, tearoff=0)
        self.var_limite = tk.IntVar(value=0)
        for kbs, rotulo in ((0, "Sem limite"), (128, "128 KB/s"),
                            (512, "512 KB/s"), (1024, "1 MB/s"),
                            (5120, "5 MB/s")):
            m_limite.add_radiobutton(label=rotulo, value=kbs,
                                     variable=self.var_limite,
                                     command=self._aplicar_limite)
        m_fila.add_cascade(label="Limite de banda", menu=m_limite)
        barra.add_cascade(label="Transferir", menu=m_fila)

        self.m_favoritos = tk.Menu(barra, tearoff=0)
        self.m_favoritos.add_command(label="Guardar a pasta atual",
                                     command=self.favoritar_atual)
        self.m_favoritos.add_separator()
        barra.add_cascade(label="Favoritos", menu=self.m_favoritos)
        self._recarregar_favoritos()

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
        self.root.bind_all("<Control-d>", lambda e: self.comparar_pastas())
        self.root.bind_all("<Control-f>", lambda e: self.procurar())

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

        self.quadro_fila = ttk.Frame(self.rodape)
        self.rodape.add(self.quadro_fila, text="Fila")

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
        # a fila precisa achar este site mesmo que ele nunca seja salvo
        self.fila.registrar_site(site)
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

    # --- fila -------------------------------------------------------------
    def _abrir_fila(self) -> None:
        self.fila = GerenciadorFila(self.gerente, ao_mudar=self._fila_mudou)
        self.fila.abrir()
        self.fila_view = FilaView(self.quadro_fila, self.fila,
                                  ao_status=self.status)
        self.fila_view.pack(fill="both", expand=True)
        self.after(250, self._tick_fila)

    # --- acoes que os paineis oferecem ------------------------------------
    def acoes_de_painel(self):
        """Entradas de menu que so a janela sabe executar.

        O FilePane recebe esta lista e nao precisa conhecer a janela: e o
        que permite o mesmo widget servir disco e servidor.
        """
        return [
            ("Editar no servidor", self._editar, True),
            ("Baixar para...", self._baixar_para, True),
            ("Guardar nos favoritos", self.favoritar, False),
            ("Procurar aqui...", self.procurar, False),
        ]

    def _status_de_thread(self, texto: str) -> None:
        self.ponte.chamar(lambda: self.status(texto))

    def _editar(self, painel) -> None:
        """Baixa o arquivo, abre no editor do sistema e vigia o salvamento."""
        selecionados = [e for e in painel.selecionados() if not e.is_dir]
        if not selecionados:
            return
        remoto = painel.nav.remote
        if remoto.is_local:
            for e in selecionados:
                EditorRemoto._abrir_no_editor(
                    remoto.nativo(remoto.juntar(painel.caminho, e.name)))
            return

        aba = self.aba_atual()
        site = getattr(aba, "site", None)
        if site is None:
            self.status("Este painel nao tem um servidor associado.")
            return
        caminhos = [remoto.juntar(painel.caminho, e.name) for e in selecionados]

        def trabalhar():
            conexao = None
            try:
                # conexao propria: a de navegacao nao pode ficar ocupada
                conexao = remotes.make_remote(site.revelado())
                conexao.conectar()
                for c in caminhos:
                    self.editor.abrir(conexao, site, c)
            except Exception as e:      # noqa: BLE001
                self._status_de_thread("Nao deu para abrir: %s" % e)
            finally:
                if conexao is not None:
                    try:
                        conexao.fechar()
                    except Exception:
                        pass

        self.status("Baixando para edicao...")
        threading.Thread(target=trabalhar, name="editar", daemon=True).start()

    def _editor_salvou(self, aberto) -> None:
        """O arquivo aberto no editor foi salvo: reenviar.

        Chamado da thread do vigia, entao a unica coisa que pode acontecer
        aqui e agendar o trabalho na thread da interface.
        """
        def enfileirar():
            import os as _os
            site = self.fila._site(aberto.site_id)
            if site is None:
                self.status("O servidor de %s nao esta mais disponivel."
                            % aberto.nome)
                return
            try:
                tamanho = _os.path.getsize(aberto.caminho_local)
            except OSError:
                return
            self._enfileirar(site, ENVIAR,
                             [(aberto.caminho_local.replace("\\", "/"),
                               aberto.caminho_remoto, tamanho, 0)])
            self.editor.confirmar_envio(aberto.caminho_local)

        self.ponte.chamar(enfileirar)

    def _baixar_para(self, painel) -> None:
        """Substitui o arrastar para fora da janela, que o Tkinter nao faz."""
        from tkinter import filedialog
        itens = [e for e in painel.selecionados() if not e.is_dir]
        if not itens:
            return
        pasta = filedialog.askdirectory(parent=self,
                                        title="Baixar para qual pasta?")
        if not pasta:
            return
        aba = self.aba_atual()
        remoto = painel.nav.remote
        destino = pasta.replace("\\", "/")
        sentido = ENVIAR if remoto.is_local else BAIXAR
        site = getattr(aba, "site", None) if sentido == BAIXAR else None
        pares = [(remoto.juntar(painel.caminho, e.name),
                  destino + "/" + e.name, e.size, e.mtime) for e in itens]
        self._enfileirar(site, sentido, pares)

    # --- favoritos --------------------------------------------------------
    def favoritar_atual(self) -> None:
        painel = self._foco()
        if painel is not None:
            self.favoritar(painel)

    def favoritar(self, painel) -> None:
        aba = self.aba_atual()
        site = getattr(aba, "site", None)
        if painel.nav.remote.is_local or site is None:
            alvo = self.gerente.extras.setdefault("favoritos_locais", [])
        else:
            alvo = site.opcoes.setdefault("favoritos", [])
        if painel.caminho not in alvo:
            alvo.append(painel.caminho)
            self.salvar_sites()
        self._recarregar_favoritos()
        self.status("Guardado nos favoritos: %s" % painel.caminho)

    def _recarregar_favoritos(self) -> None:
        menu = self.m_favoritos
        menu.delete(2, "end")
        vazio = True
        for caminho in self.gerente.extras.get("favoritos_locais", []):
            menu.add_command(label="Local: %s" % util.elidir(caminho, 50),
                             command=lambda c=caminho: self._ir_favorito(c, None))
            vazio = False
        for site in self.gerente:
            for caminho in (site.opcoes.get("favoritos") or []):
                menu.add_command(
                    label="%s: %s" % (site.nome, util.elidir(caminho, 40)),
                    command=lambda c=caminho, s=site: self._ir_favorito(c, s))
                vazio = False
        if vazio:
            menu.add_command(label="(nenhum ainda)", state="disabled")

    def _ir_favorito(self, caminho: str, site) -> None:
        aba = self.aba_atual()
        if site is None:
            aba.esquerda.ir_para(caminho)
            return
        atual = getattr(aba, "site", None)
        if atual is not None and atual.id == site.id and aba.direita is not None:
            aba.direita.ir_para(caminho)
        else:
            site.pasta_remota = caminho
            self.abrir_site(site)

    # --- busca ------------------------------------------------------------
    def procurar(self, painel=None) -> None:
        aba = self.aba_atual()
        site = getattr(aba, "site", None)
        if painel is None:
            painel = aba.direita if aba is not None else None
        if site is None or painel is None or painel.nav.remote.is_local:
            self.status("A busca recursiva e para o painel do servidor.")
            return
        BuscaDialog(self.root, self, painel, site)

    # --- arrastar ---------------------------------------------------------
    def ligar_arrastar(self, aba) -> None:
        """Arrasto entre paineis e, quando houver tkinterdnd2, do Explorer."""
        for painel in (aba.esquerda, aba.direita):
            if painel is None or getattr(painel, "_arrasto_ligado", False):
                continue
            painel._arrasto_ligado = True
            dnd.ArrastoInterno(
                painel.tree,
                obter_itens=painel.selecionados,
                ao_soltar=lambda alvo, itens, p=painel: self._soltou(p, alvo,
                                                                     itens))
            dnd.registrar_alvo(
                painel.tree,
                lambda caminhos, p=painel: self._soltou_do_sistema(p, caminhos))

    def _soltou(self, origem, alvo_widget, itens) -> None:
        """Soltou dentro da janela: no outro painel, ou na fila."""
        aba = self.aba_atual()
        if aba is None or not itens:
            return
        caminho_alvo = str(alvo_widget)
        for painel in (aba.esquerda, aba.direita):
            if painel is None or painel is origem:
                continue
            if caminho_alvo.startswith(str(painel)):
                self.transferir(origem, itens)
                return
        if caminho_alvo.startswith(str(self.fila_view)):
            self.transferir(origem, itens)

    def _soltou_do_sistema(self, painel, caminhos) -> None:
        """Arquivos arrastados do Explorer: sobem para o painel do servidor."""
        import os as _os
        aba = self.aba_atual()
        if aba is None or aba.direita is None or aba.direita.nav.remote.is_local:
            self.status("Conecte-se a um servidor para soltar arquivos nele.")
            return
        remoto = aba.direita.nav.remote
        pares = []
        for caminho in caminhos:
            if _os.path.isdir(caminho):
                continue      # pasta inteira exige varredura: use o menu
            try:
                tamanho = _os.path.getsize(caminho)
                quando = _os.path.getmtime(caminho)
            except OSError:
                continue
            pares.append((caminho.replace("\\", "/"),
                          remoto.juntar(aba.direita.caminho,
                                        _os.path.basename(caminho)),
                          tamanho, quando))
        if pares:
            self._enfileirar(getattr(aba, "site", None), ENVIAR, pares)
        else:
            self.status("Solte arquivos; pastas inteiras, pelo menu.")

    # --- comparacao -------------------------------------------------------
    def _dois_paineis(self):
        aba = self.aba_atual()
        if aba is None or aba.direita is None:
            self.status("Abra os dois lados antes de comparar.")
            return None
        return aba

    def comparar_pastas(self) -> None:
        aba = self._dois_paineis()
        if aba is not None:
            self.status("Comparando...")
            comparar_nos_paineis(self, aba.esquerda, aba.direita)

    def sincronizar(self) -> None:
        aba = self._dois_paineis()
        if aba is not None:
            SincronizarDialog(self.root, self, aba.esquerda, aba.direita,
                              getattr(aba, "site", None))

    def _aplicar_limite(self) -> None:
        kbs = self.var_limite.get()
        self.fila.definir_limite(kbs)
        self.status("Limite de banda: %s."
                    % ("sem limite" if not kbs else util.fmt_velocidade(kbs * 1024)))

    def _fila_mudou(self) -> None:
        """Chamado das threads da fila: so levanta a bandeira.

        Tocar em widget daqui seria tocar em Tk de outra thread, que e o
        jeito classico de travar um programa Tkinter de forma irreproduzivel.
        """
        try:
            self.fila_view.marcar_sujo()
        except Exception:
            pass

    def _tick_fila(self) -> None:
        """Barra de status global. Le o agregado, nunca item por item."""
        if not self._vivo:
            return
        try:
            r = self.fila.resumo()
            if r["ativos"]:
                self.pb.configure(value=r["pct"])
                self.lb_taxa.configure(
                    text="%s  %s restante" % (util.fmt_velocidade(r["velocidade"]),
                                              util.fmt_tempo(r["eta"])))
            else:
                self.pb.configure(value=0)
                self.lb_taxa.configure(text="")
            self.after(250, self._tick_fila)
        except tk.TclError:
            self._vivo = False

    def transferir(self, origem, itens) -> None:
        """Manda a selecao de um painel para o outro, pela fila."""
        aba = self.aba_atual()
        if aba is None:
            return
        destino = (aba.direita if origem is aba.esquerda else aba.esquerda)
        if destino is None:
            self.status("Conecte-se a um servidor para transferir.")
            return

        remoto_origem = origem.nav.remote
        remoto_destino = destino.nav.remote
        sentido = ENVIAR if remoto_origem.is_local else BAIXAR
        site = getattr(aba, "site", None)
        pasta_destino = destino.caminho
        caminhos = [remoto_origem.juntar(origem.caminho, e.name) for e in itens]
        tem_pasta = any(e.is_dir for e in itens)

        if not tem_pasta:
            pares = [(remoto_origem.juntar(origem.caminho, e.name),
                      remoto_destino.juntar(pasta_destino, e.name),
                      e.size, e.mtime) for e in itens]
            self._enfileirar(site, sentido, pares)
            return

        # ha pasta na selecao: percorrer pode demorar, entao vai para uma
        # thread e a janela continua respondendo
        self.status("Lendo as pastas selecionadas...")

        def trabalhar():
            try:
                if remoto_origem.is_local:
                    leitor, fechar = remoto_origem, False
                else:
                    leitor, fechar = remotes.make_remote(site.revelado()), True
                    leitor.conectar()
                try:
                    pares = self.fila.expandir(leitor, sentido, caminhos,
                                               pasta_destino, remoto_destino)
                finally:
                    if fechar:
                        leitor.fechar()
            except Exception as e:      # noqa: BLE001
                self.ponte.chamar(
                    lambda: self.status("Nao deu para ler a pasta: %s" % e))
                return
            self.ponte.chamar(lambda: self._enfileirar(site, sentido, pares))

        threading.Thread(target=trabalhar, name="expandir", daemon=True).start()

    def _enfileirar(self, site, sentido: str, pares) -> None:
        if not pares:
            self.status("Nada para transferir.")
            return
        itens = self.fila.montar_itens(site, sentido, pares)
        self.fila.enfileirar(itens)
        self.fila_view.marcar_sujo()
        self.rodape.select(self.quadro_fila)
        self.status("%d arquivo(s) na fila." % len(itens))

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
        self.fila_view.retema()
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
        self.ponte.parar()
        for aba in self._abas:
            try:
                aba.fechar()
            except Exception:
                pass
        try:
            self.editor.fechar()
        except Exception:
            pass
        try:
            # fecha a fila ANTES da janela: as threads precisam gravar o
            # progresso no banco, e e isso que permite retomar depois
            self.fila.fechar()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def abrir(tema_nome: str = tema.PADRAO) -> None:
    # a raiz precisa ser decidida antes de qualquer widget existir: o
    # tkinterdnd2 exige a classe dele na propria raiz
    root = dnd.criar_raiz()
    JanelaPrincipal(root, tema_nome)
    root.mainloop()
