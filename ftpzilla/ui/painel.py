"""FilePane: um painel de arquivos. O mesmo widget serve disco e servidor.

Ele so conhece um Navegador (browser.py), nunca um Remote direto, entao nao
sabe nem se a listagem veio do disco ou de um FTP do outro lado do mundo.

Limite conhecido do Tkinter: ttk.Treeview NAO tem modo virtual. Inserir
dezenas de milhares de linhas de uma vez trava a interface por segundos.
As tres defesas aqui sao deliberadas:

  1. insercao em lotes de LOTE linhas, com after_idle entre eles, para a
     janela continuar respondendo (e o usuario poder cancelar);
  2. acima de LIMITE_ICONE itens o icone e omitido - desenhar imagem e
     cerca de metade do custo de cada linha;
  3. acima de LIMITE_AVISO o painel para e pede um filtro, em vez de fingir
     que da conta.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Dict, List, Optional

from .. import util
from ..browser import Navegador, entradas_ordenadas
from ..remotes.base import Entry
from . import icones, tema

LOTE = 500              # linhas inseridas por vez
LIMITE_ICONE = 5000     # acima disso, sem icone
LIMITE_AVISO = 20000    # acima disso, exige filtro

COLUNAS = (
    ("tamanho", "Tamanho", 90, "e"),
    ("tipo", "Tipo", 70, "w"),
    ("modificado", "Modificado", 130, "w"),
    ("permissoes", "Perm.", 70, "w"),
)


class FilePane(ttk.Frame):
    """Um lado da janela: barra de caminho + lista de arquivos."""

    def __init__(self, master, navegador: Navegador, titulo: str = "",
                 ao_mudar_pasta: Optional[Callable] = None,
                 ao_transferir: Optional[Callable] = None,
                 ao_status: Optional[Callable] = None,
                 ao_erro: Optional[Callable] = None,
                 acoes_extras: Optional[List] = None):
        super().__init__(master, padding=(4, 4))
        self.nav = navegador
        self.titulo = titulo
        self.ao_mudar_pasta = ao_mudar_pasta
        self.ao_transferir = ao_transferir
        self.ao_status = ao_status
        self.ao_erro = ao_erro
        # [(rotulo, funcao(painel), precisa_selecao)] - a janela acrescenta
        # aqui o que depende dela (editar no servidor, favoritos, procurar),
        # e o painel continua sem conhecer a janela
        self.acoes_extras = list(acoes_extras or [])

        self.caminho = "/"
        self._itens: List[Entry] = []          # listagem crua, sem filtro
        self._visiveis: List[Entry] = []       # o que esta na Treeview
        self._por_iid: Dict[str, Entry] = {}
        self._ordem = ("nome", False)          # (coluna, invertido)
        self._historico: List[str] = []
        self._comparacao: Dict[str, str] = {}  # nome -> tag (usado no M7)
        self._pendente = None                  # id do after_idle da insercao
        self._carregando = False
        self._com_icone = True

        self._montar()

    # ------------------------------------------------------------------
    # Construcao
    # ------------------------------------------------------------------
    def _montar(self) -> None:
        topo = ttk.Frame(self)
        topo.pack(fill="x")

        if self.titulo:
            ttk.Label(topo, text=self.titulo, style="Accent.TLabel").pack(side="left",
                                                                         padx=(0, 6))

        self.var_caminho = tk.StringVar()
        self.cb_caminho = ttk.Combobox(topo, textvariable=self.var_caminho,
                                       values=[])
        self.cb_caminho.pack(side="left", fill="x", expand=True)
        self.cb_caminho.bind("<Return>", lambda e: self.ir_para(self.var_caminho.get()))
        self.cb_caminho.bind("<<ComboboxSelected>>",
                             lambda e: self.ir_para(self.var_caminho.get()))

        # o seletor de pastas do Windows so existe para o painel local: no
        # servidor nao ha como o dialogo do sistema navegar, e la quem faz
        # esse papel e a busca recursiva (Ctrl+F)
        if getattr(self.nav.remote, "is_local", False):
            self.bt_procurar = ttk.Button(topo, text="...", width=3,
                                          command=self.escolher_pasta)
            self.bt_procurar.pack(side="left", padx=(4, 0))

        self.bt_acima = ttk.Button(topo, text="↑", width=3, command=self.subir)
        self.bt_acima.pack(side="left", padx=(4, 0))
        ttk.Button(topo, text="↻", width=3,
                   command=self.recarregar).pack(side="left", padx=(2, 0))

        meio = ttk.Frame(self)
        meio.pack(fill="both", expand=True, pady=(4, 2))

        colunas = tuple(c[0] for c in COLUNAS)
        self.tree = ttk.Treeview(meio, columns=colunas, selectmode="extended")
        self.tree.heading("#0", text="Nome",
                          command=lambda: self._ordenar_por("nome"))
        self.tree.column("#0", width=240, minwidth=120, stretch=True)
        for chave, rotulo, largura, anchor in COLUNAS:
            self.tree.heading(chave, text=rotulo,
                              command=lambda c=chave: self._ordenar_por(c))
            self.tree.column(chave, width=largura, minwidth=40, anchor=anchor,
                             stretch=False)

        barra = ttk.Scrollbar(meio, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barra.set)
        self.tree.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")

        rodape = ttk.Frame(self)
        rodape.pack(fill="x")
        ttk.Label(rodape, text="Filtro:", style="Fraco.TLabel").pack(side="left")
        self.var_filtro = tk.StringVar()
        ent = ttk.Entry(rodape, textvariable=self.var_filtro, width=14)
        ent.pack(side="left", padx=(3, 6))
        self.var_filtro.trace_add("write", lambda *_: self._redesenhar())
        self.lb_resumo = ttk.Label(rodape, text="", style="Fraco.TLabel")
        self.lb_resumo.pack(side="left", fill="x", expand=True)

        self._montar_menu()
        self._ligar_teclas()
        tema.aplicar_tags(self.tree)

    def _montar_menu(self) -> None:
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Abrir", command=self._abrir_selecao)
        self.menu.add_command(label="Transferir", command=self._transferir_selecao)
        self.menu.add_separator()
        self.menu.add_command(label="Nova pasta...", command=self.nova_pasta)
        self.menu.add_command(label="Renomear...", command=self.renomear)
        self.menu.add_command(label="Apagar", command=self.apagar)
        self.menu.add_separator()
        self.menu.add_command(label="Copiar caminho", command=self.copiar_caminho)
        self.menu.add_command(label="Atualizar", command=self.recarregar)
        if self.acoes_extras:
            self.menu.add_separator()
            for rotulo, funcao, _precisa in self.acoes_extras:
                self.menu.add_command(label=rotulo,
                                      command=lambda f=funcao: f(self))

    def _ligar_teclas(self) -> None:
        self.tree.bind("<Double-1>", self._duplo_clique)
        self.tree.bind("<Return>", lambda e: self._abrir_selecao())
        self.tree.bind("<BackSpace>", lambda e: self.subir())
        self.tree.bind("<F5>", lambda e: self.recarregar())
        self.tree.bind("<F2>", lambda e: self.renomear())
        self.tree.bind("<Delete>", lambda e: self.apagar())
        self.tree.bind("<Button-3>", self._menu_contexto)

    # ------------------------------------------------------------------
    # Navegacao
    # ------------------------------------------------------------------
    def ir_para(self, caminho: str, guardar: bool = True) -> None:
        destino = self.nav.remote.normalizar(caminho)
        if guardar and self.caminho and self.caminho != destino:
            self._historico.append(self.caminho)
            del self._historico[:-50]
        self.caminho = destino
        self.var_caminho.set(destino)
        self.recarregar()

    def escolher_pasta(self) -> None:
        """Abre o seletor de pastas do sistema, partindo da pasta atual.

        Digitar caminho a mao e o pior jeito de achar uma pasta, e o dialogo
        do proprio Windows ja tem busca, atalhos e unidades de rede.
        """
        remoto = self.nav.remote
        inicial = ""
        try:
            inicial = remoto.nativo(self.caminho)
        except Exception:       # noqa: BLE001 - so um ponto de partida
            inicial = ""
        escolhida = filedialog.askdirectory(
            parent=self, title="Escolher a pasta local",
            initialdir=inicial or None, mustexist=True)
        if escolhida:
            self.ir_para(escolhida)

    def subir(self) -> None:
        pai = self.nav.remote.pai(self.caminho)
        if pai != self.caminho:
            self.ir_para(pai)

    def voltar(self) -> None:
        if self._historico:
            self.ir_para(self._historico.pop(), guardar=False)

    def recarregar(self) -> None:
        geracao = self.nav.nova_geracao()
        self._carregando = True
        self._status("Lendo %s..." % util.elidir(self.caminho, 50))
        self.nav.listar(self.caminho, ok=self._recebeu, erro=self._falhou,
                        geracao=geracao)

    def _recebeu(self, itens: List[Entry], geracao: int) -> None:
        if geracao != self.nav.geracao:
            return          # o usuario ja navegou para outro lugar
        self._carregando = False
        self._itens = list(itens)
        self._lembrar_caminho()
        self._redesenhar()

    def _falhou(self, exc: Exception) -> None:
        self._carregando = False
        self._status("Falhou: %s" % exc)
        if self.ao_erro is not None:
            self.ao_erro(exc)
        else:
            messagebox.showerror("FTPZilla", str(exc), parent=self)

    def _lembrar_caminho(self) -> None:
        valores = list(self.cb_caminho["values"])
        if self.caminho in valores:
            valores.remove(self.caminho)
        valores.insert(0, self.caminho)
        self.cb_caminho["values"] = valores[:25]

    # ------------------------------------------------------------------
    # Desenho da lista
    # ------------------------------------------------------------------
    def _filtrados(self) -> List[Entry]:
        """Filtra ANTES de inserir: filtrar depois seria pagar o custo da
        Treeview por linhas que ninguem vai ver."""
        texto = self.var_filtro.get().strip().casefold()
        itens = self._itens
        if texto:
            itens = [e for e in itens if texto in e.name.casefold()]
        coluna, invertido = self._ordem
        return entradas_ordenadas(itens, coluna, invertido)

    def _redesenhar(self) -> None:
        if self._pendente is not None:
            try:
                self.after_cancel(self._pendente)
            except tk.TclError:
                pass
            self._pendente = None

        self.tree.delete(*self.tree.get_children())
        self._por_iid.clear()
        self._visiveis = self._filtrados()

        if len(self._visiveis) > LIMITE_AVISO and not self.var_filtro.get().strip():
            self._status("%d itens: use o filtro para reduzir a lista."
                         % len(self._visiveis))
            self.tree.insert("", "end", iid="__aviso__",
                             text="Pasta muito grande (%d itens). "
                                  "Digite algo no filtro." % len(self._visiveis))
            return

        self._com_icone = len(self._visiveis) <= LIMITE_ICONE
        self._inserir_lote(0)

    def _inserir_lote(self, inicio: int) -> None:
        fim = min(inicio + LOTE, len(self._visiveis))
        for i in range(inicio, fim):
            self._inserir(self._visiveis[i], i)
        if fim < len(self._visiveis):
            self._status("Carregando %d/%d..." % (fim, len(self._visiveis)))
            self._pendente = self.after_idle(self._inserir_lote, fim)
        else:
            self._pendente = None
            self._resumo()

    def _inserir(self, e: Entry, indice: int) -> None:
        iid = "i%d" % indice
        tags = []
        if e.name in self._comparacao:
            tags.append(self._comparacao[e.name])
        elif e.is_dir:
            tags.append("pasta")
        valores = (
            "" if e.is_dir else util.fmt_bytes(e.size),
            "Pasta" if e.is_dir else (e.ext.upper() or "Arquivo"),
            util.fmt_data(e.mtime),
            util.fmt_perms(e.perms),
        )
        img = icones.para_entry(e) if self._com_icone else None
        if img is not None:
            self.tree.insert("", "end", iid=iid, text=e.name, image=img,
                             values=valores, tags=tags)
        else:
            self.tree.insert("", "end", iid=iid, text=e.name, values=valores,
                             tags=tags)
        self._por_iid[iid] = e

    def _resumo(self) -> None:
        pastas = sum(1 for e in self._visiveis if e.is_dir)
        arquivos = len(self._visiveis) - pastas
        total = sum(e.size for e in self._visiveis if not e.is_dir)
        escondidos = len(self._itens) - len(self._visiveis)
        texto = "%d pasta(s), %d arquivo(s), %s" % (pastas, arquivos,
                                                    util.fmt_bytes(total))
        if escondidos > 0:
            texto += "  (%d ocultos pelo filtro)" % escondidos
        self.lb_resumo.configure(text=texto)
        self._status(texto)

    def _ordenar_por(self, coluna: str) -> None:
        atual, invertido = self._ordem
        self._ordem = (coluna, not invertido if coluna == atual else False)
        self._redesenhar()

    def aplicar_comparacao(self, mapa: Dict[str, str]) -> None:
        """Pinta as linhas conforme o resultado da comparacao (M7)."""
        self._comparacao = dict(mapa or {})
        self._redesenhar()

    # ------------------------------------------------------------------
    # Selecao e acoes
    # ------------------------------------------------------------------
    def selecionados(self) -> List[Entry]:
        return [self._por_iid[i] for i in self.tree.selection()
                if i in self._por_iid]

    def caminhos_selecionados(self) -> List[str]:
        return [self.nav.remote.juntar(self.caminho, e.name)
                for e in self.selecionados()]

    def _duplo_clique(self, evento) -> None:
        iid = self.tree.identify_row(evento.y)
        if not iid or iid not in self._por_iid:
            return
        self.tree.selection_set(iid)
        self._abrir_selecao()

    def _abrir_selecao(self) -> None:
        sel = self.selecionados()
        if not sel:
            return
        e = sel[0]
        if e.is_dir:
            self.ir_para(self.nav.remote.juntar(self.caminho, e.name))
        else:
            self._transferir_selecao()

    def _transferir_selecao(self) -> None:
        sel = self.selecionados()
        if sel and self.ao_transferir is not None:
            self.ao_transferir(self, sel)

    def _menu_contexto(self, evento) -> None:
        iid = self.tree.identify_row(evento.y)
        # se o clique caiu fora da selecao, seleciona o item sob o cursor:
        # senao a acao do menu executa em um arquivo que o usuario nao esta
        # olhando - erro classico, e destrutivo quando a acao e "apagar"
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if iid:
            self.tree.focus(iid)
        tem = bool(self.selecionados())
        desabilitaveis = ["Abrir", "Transferir", "Renomear...", "Apagar"]
        desabilitaveis += [r for r, _f, precisa in self.acoes_extras if precisa]
        for rotulo in desabilitaveis:
            self.menu.entryconfigure(rotulo, state="normal" if tem else "disabled")
        try:
            self.menu.tk_popup(evento.x_root, evento.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------
    # Operacoes de arquivo
    # ------------------------------------------------------------------
    def nova_pasta(self) -> None:
        nome = simpledialog.askstring("Nova pasta", "Nome da pasta:", parent=self)
        if not nome:
            return
        alvo = self.nav.remote.juntar(self.caminho, nome.strip())
        self.nav.criar_pasta(alvo, ok=lambda *_: self.recarregar(),
                             erro=self._falhou)

    def renomear(self) -> None:
        sel = self.selecionados()
        if not sel:
            return
        e = sel[0]
        novo = simpledialog.askstring("Renomear", "Novo nome:",
                                      initialvalue=e.name, parent=self)
        if not novo or novo == e.name:
            return
        de = self.nav.remote.juntar(self.caminho, e.name)
        para = self.nav.remote.juntar(self.caminho, novo.strip())
        self.nav.renomear(de, para, ok=lambda *_: self.recarregar(),
                          erro=self._falhou)

    def apagar(self) -> None:
        sel = self.selecionados()
        if not sel:
            return
        pastas = [e for e in sel if e.is_dir]
        if len(sel) == 1:
            pergunta = "Apagar %s \"%s\"?" % ("a pasta" if pastas else "o arquivo",
                                              sel[0].name)
        else:
            pergunta = "Apagar %d itens?" % len(sel)
        if pastas:
            pergunta += "\n\nPastas sao apagadas com todo o conteudo."
        if not messagebox.askyesno("Apagar", pergunta, parent=self, icon="warning"):
            return
        caminhos = self.caminhos_selecionados()
        self.nav.apagar(caminhos, ok=lambda *_: self.recarregar(),
                        erro=self._falhou,
                        progresso=lambda c: self._status("Apagando %s" % c))

    def copiar_caminho(self) -> None:
        sel = self.caminhos_selecionados() or [self.caminho]
        texto = "\n".join(sel)
        self.clipboard_clear()
        self.clipboard_append(texto)
        self._status("Caminho copiado.")

    # ------------------------------------------------------------------
    def _status(self, texto: str) -> None:
        if self.ao_status is not None:
            self.ao_status(self.titulo, texto)

    def retema(self) -> None:
        """Rechama depois de trocar o tema: recria icones e tags."""
        icones.limpar()
        tema.aplicar_tags(self.tree)
        tema.pintar_classico(self)
        self._redesenhar()
