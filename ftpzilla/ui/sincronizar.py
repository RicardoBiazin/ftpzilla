"""Comparar pastas e sincronizar, com pre-visualizacao.

Regra desta tela: nada acontece antes de a pessoa ver a lista do que vai
acontecer. Sincronizacao apaga arquivo; um clique errado num programa que
"simplesmente executa" custa trabalho de verdade. Por isso a analise e um
passo separado do executar, e cada linha pode ser desmarcada.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List

from .. import compare, log, util
from ..compare import Comparador
from ..fila_store import BAIXAR, ENVIAR
from ..remotes.base import Cancelado, ErroRemoto
from . import tema

logger = log.get()

DIRECOES = [
    (compare.ENVIAR_NOVOS, "Enviar o que falta e o que mudou (local -> servidor)"),
    (compare.BAIXAR_NOVOS, "Baixar o que falta e o que mudou (servidor -> local)"),
    (compare.ESPELHAR_ENVIO, "Espelhar no servidor (APAGA o que sobrar la)"),
    (compare.ESPELHAR_BAIXA, "Espelhar no disco (APAGA o que sobrar aqui)"),
    (compare.BIDIRECIONAL, "Nos dois sentidos, sempre a versao mais nova"),
]

ROTULO_ACAO = {
    "enviar": "Enviar",
    "baixar": "Baixar",
    "criar_pasta_esq": "Criar pasta aqui",
    "criar_pasta_dir": "Criar pasta la",
    "apagar_esq": "APAGAR daqui",
    "apagar_dir": "APAGAR de la",
}


class SincronizarDialog(tk.Toplevel):

    def __init__(self, master, janela, esquerda, direita, site):
        super().__init__(master)
        self.title("Sincronizar pastas")
        self.transient(master)
        self.janela = janela
        self.pane_esq = esquerda
        self.pane_dir = direita
        self.site = site
        self.acoes: List[dict] = []
        self._marcados = set()
        self._cancelar = threading.Event()

        corpo = ttk.Frame(self, padding=10)
        corpo.pack(fill="both", expand=True)

        ttk.Label(corpo, text="Local:  %s" % util.elidir(esquerda.caminho, 70),
                  style="Fraco.TLabel").pack(anchor="w")
        ttk.Label(corpo, text="Servidor:  %s" % util.elidir(direita.caminho, 70),
                  style="Fraco.TLabel").pack(anchor="w", pady=(0, 8))

        linha = ttk.Frame(corpo)
        linha.pack(fill="x")
        ttk.Label(linha, text="O que fazer:").pack(side="left")
        self._rotulos = {r: k for k, r in DIRECOES}
        self.cb_direcao = ttk.Combobox(linha, values=[r for _, r in DIRECOES],
                                       state="readonly", width=52)
        self.cb_direcao.current(0)
        self.cb_direcao.pack(side="left", padx=6)

        linha2 = ttk.Frame(corpo)
        linha2.pack(fill="x", pady=(6, 0))
        ttk.Label(linha2, text="Ignorar (padroes separados por ;):").pack(side="left")
        self.var_excluir = tk.StringVar(value="*.tmp;~$*;Thumbs.db;desktop.ini")
        ttk.Entry(linha2, textvariable=self.var_excluir, width=40).pack(
            side="left", padx=6, fill="x", expand=True)

        ttk.Button(corpo, text="Analisar", command=self.analisar).pack(
            anchor="w", pady=8)

        quadro = ttk.Frame(corpo)
        quadro.pack(fill="both", expand=True)
        colunas = ("acao", "arquivo", "tamanho", "motivo")
        self.tree = ttk.Treeview(quadro, columns=colunas, show="headings",
                                 selectmode="extended", height=14)
        for chave, rotulo, largura in (("acao", "Acao", 130),
                                       ("arquivo", "Arquivo", 320),
                                       ("tamanho", "Tamanho", 90),
                                       ("motivo", "Por que", 150)):
            self.tree.heading(chave, text=rotulo)
            self.tree.column(chave, width=largura,
                             anchor="e" if chave == "tamanho" else "w")
        rolar = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=rolar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        rolar.pack(side="right", fill="y")
        tema.aplicar_tags(self.tree)
        self.tree.bind("<space>", lambda e: self.alternar())
        self.tree.bind("<Double-1>", lambda e: self.alternar())

        self.lb_resumo = ttk.Label(corpo, text="Clique em Analisar.",
                                   style="Fraco.TLabel")
        self.lb_resumo.pack(anchor="w", pady=(6, 0))

        rodape = ttk.Frame(corpo)
        rodape.pack(fill="x", pady=(8, 0))
        ttk.Button(rodape, text="Marcar/desmarcar",
                   command=self.alternar).pack(side="left")
        self.bt_executar = ttk.Button(rodape, text="Executar",
                                      command=self.executar, state="disabled")
        self.bt_executar.pack(side="right")
        ttk.Button(rodape, text="Fechar", command=self.destroy).pack(
            side="right", padx=6)

        tema.pintar_classico(self)
        tema.center_over(self, master)
        self.grab_set()

    # ------------------------------------------------------------------
    def _comparador(self) -> Comparador:
        excluir = [p.strip() for p in self.var_excluir.get().split(";")
                   if p.strip()]
        return Comparador(self.pane_esq.nav.remote, self.pane_dir.nav.remote,
                          excluir=excluir)

    def analisar(self) -> None:
        self.lb_resumo.configure(text="Lendo as duas pastas...")
        self.bt_executar.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())

        # TUDO que vem de widget e lido AQUI, na thread do Tkinter. Ler uma
        # StringVar de dentro da thread de trabalho e tocar em Tk de outra
        # thread - as vezes funciona, as vezes derruba a thread em silencio,
        # e ai a analise simplesmente nunca termina.
        direcao = self._rotulos[self.cb_direcao.get()]
        cam_esq, cam_dir = self.pane_esq.caminho, self.pane_dir.caminho
        comparador = self._comparador()
        resposta: "queue.Queue" = queue.Queue()

        def trabalhar():
            try:
                # uma conexao propria: a de navegacao esta ocupada com a
                # interface, e varrer a arvore pode demorar
                remoto, fechar = self._conexao_para_leitura()
                try:
                    comparador.dir = remoto
                    pares = list(comparador.comparar_arvore(cam_esq, cam_dir))
                    acoes = comparador.plano(pares, direcao, cam_esq, cam_dir)
                finally:
                    if fechar:
                        remoto.fechar()
            except BaseException as e:      # noqa: BLE001
                resposta.put(("erro", e))
                return
            resposta.put(("ok", acoes))

        threading.Thread(target=trabalhar, name="comparar", daemon=True).start()
        self._esperar(resposta)

    def _esperar(self, resposta: "queue.Queue") -> None:
        """Poll na thread do Tkinter: e o unico lugar que pode tocar widget."""
        try:
            tipo, valor = resposta.get_nowait()
        except queue.Empty:
            try:
                self.after(80, self._esperar, resposta)
            except tk.TclError:
                pass
            return
        if tipo == "erro":
            self._falhou(valor)
        else:
            self._mostrar(valor)

    def _conexao_para_leitura(self):
        remoto = self.pane_dir.nav.remote
        if remoto.is_local or self.site is None:
            return remoto, False
        from .. import remotes
        novo = remotes.make_remote(self.site.revelado())
        novo.conectar()
        return novo, True

    def _falhou(self, exc) -> None:
        self.lb_resumo.configure(text="Nao deu para comparar: %s" % exc)
        logger.error("Comparacao falhou: %s", exc)

    def _mostrar(self, acoes: List[dict]) -> None:
        self.acoes = acoes
        self._marcados = set(range(len(acoes)))
        for i, a in enumerate(acoes):
            par = a["par"]
            entrada = par.esquerda or par.direita
            tamanho = "" if par.is_dir else util.fmt_bytes(entrada.size)
            tag = "conflito" if a["acao"].startswith("apagar") else (
                "so_esquerda" if a["acao"] == "enviar" else "esq_mais_nova")
            self.tree.insert("", "end", iid=str(i),
                             values=("✓ " + ROTULO_ACAO.get(a["acao"],
                                                                 a["acao"]),
                                     par.rel, tamanho, _motivo(par.estado)),
                             tags=(tag,))
        self.lb_resumo.configure(text=compare.resumir_plano(acoes))
        self.bt_executar.configure(
            state="normal" if acoes else "disabled")
        if not acoes:
            self.lb_resumo.configure(text="As duas pastas ja estao iguais.")

    def alternar(self) -> None:
        for iid in self.tree.selection():
            i = int(iid)
            valores = list(self.tree.item(iid, "values"))
            if i in self._marcados:
                self._marcados.discard(i)
                valores[0] = valores[0].replace("✓ ", "– ")
            else:
                self._marcados.add(i)
                valores[0] = valores[0].replace("– ", "✓ ")
            self.tree.item(iid, values=valores)
        self.lb_resumo.configure(text="%d de %d acoes marcadas."
                                 % (len(self._marcados), len(self.acoes)))

    # ------------------------------------------------------------------
    def executar(self) -> None:
        escolhidas = [a for i, a in enumerate(self.acoes)
                      if i in self._marcados]
        if not escolhidas:
            return
        apagando = [a for a in escolhidas if a["acao"].startswith("apagar")]
        if apagando:
            texto = ("%d item(ns) serao APAGADOS e nao vao para a lixeira.\n\n"
                     "Os primeiros:\n%s\n\nConfirma?"
                     % (len(apagando),
                        "\n".join("  " + a["par"].rel for a in apagando[:8])))
            if not messagebox.askyesno("Apagar arquivos", texto, icon="warning",
                                       parent=self):
                return

        pares_envio, pares_baixa = [], []
        for a in escolhidas:
            par = a["par"]
            entrada = par.esquerda or par.direita
            if a["acao"] == "enviar":
                pares_envio.append((a["esquerda"], a["direita"],
                                    entrada.size, entrada.mtime))
            elif a["acao"] == "baixar":
                pares_baixa.append((a["direita"], a["esquerda"],
                                    entrada.size, entrada.mtime))

        if pares_envio:
            self.janela._enfileirar(self.site, ENVIAR, pares_envio)
        if pares_baixa:
            self.janela._enfileirar(self.site, BAIXAR, pares_baixa)

        estruturais = [a for a in escolhidas
                       if a["acao"].startswith(("criar", "apagar"))]
        if estruturais:
            self._aplicar_estruturais(estruturais)

        self.destroy()

    def _aplicar_estruturais(self, acoes: List[dict]) -> None:
        """Criar e apagar pastas/arquivos fora da fila.

        Nao sao transferencias: nao tem progresso nem retomada, e precisam
        acontecer na ordem em que o plano as colocou (pasta antes do arquivo
        ao criar, arquivo antes da pasta ao apagar).
        """
        esq = self.pane_esq.nav.remote

        def trabalhar():
            remoto, fechar = self._conexao_para_leitura()
            feitas = 0
            try:
                for a in acoes:
                    alvo = esq if a["acao"].endswith("_esq") else remoto
                    caminho = (a["esquerda"] if a["acao"].endswith("_esq")
                               else a["direita"])
                    try:
                        if a["acao"].startswith("criar"):
                            alvo.criar_pastas(caminho)
                        elif a["par"].is_dir:
                            alvo.apagar_arvore(caminho)
                        else:
                            alvo.apagar_arquivo(caminho)
                        feitas += 1
                    except ErroRemoto as e:
                        logger.error("%s em %s: %s", a["acao"], caminho, e)
            finally:
                if fechar:
                    remoto.fechar()
            self.janela.ponte.chamar(lambda: (
                self.janela.status("%d pasta(s)/arquivo(s) ajustados." % feitas),
                self.pane_esq.recarregar(), self.pane_dir.recarregar()))

        threading.Thread(target=trabalhar, name="sincronizar",
                         daemon=True).start()


def _motivo(estado: str) -> str:
    return {
        compare.SO_ESQUERDA: "so existe aqui",
        compare.SO_DIREITA: "so existe la",
        compare.ESQ_MAIS_NOVA: "a daqui e mais nova",
        compare.DIR_MAIS_NOVA: "a de la e mais nova",
        compare.TAMANHO_DIFERE: "tamanho diferente",
        compare.TIPO_DIFERE: "um e pasta, o outro e arquivo",
        compare.IGUAL: "iguais",
    }.get(estado, estado)


def comparar_nos_paineis(janela, esquerda, direita) -> None:
    """Colore os dois paineis com o resultado da comparacao de UM nivel.

    So um nivel de proposito: e a pasta que a pessoa esta olhando. Varrer a
    arvore inteira a cada troca de pasta seria lento e ninguem pediu.
    """
    cam_esq, cam_dir = esquerda.caminho, direita.caminho
    comparador = Comparador(esquerda.nav.remote, direita.nav.remote)
    resposta: "queue.Queue" = queue.Queue()

    def trabalhar():
        try:
            pares = comparador.comparar_pasta(cam_esq, cam_dir)
            resposta.put(("ok", (comparador.mapa_para_painel(pares),
                                 compare.resumir(pares))))
        except BaseException as e:      # noqa: BLE001
            resposta.put(("erro", e))

    def receber():
        try:
            tipo, valor = resposta.get_nowait()
        except queue.Empty:
            try:
                janela.after(80, receber)
            except tk.TclError:
                pass
            return
        if tipo == "erro":
            janela.status("Nao deu para comparar: %s" % valor)
            return
        mapa, resumo = valor
        esquerda.aplicar_comparacao(mapa)
        direita.aplicar_comparacao(mapa)
        diferentes = sum(n for k, n in resumo.items() if k != compare.IGUAL)
        janela.status("Comparacao: %d diferenca(s) nesta pasta." % diferentes)

    threading.Thread(target=trabalhar, name="comparar-painel",
                     daemon=True).start()
    receber()
