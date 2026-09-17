"""Comparacao de pastas e geracao do plano de sincronizacao.

A regra mais importante deste arquivo cabe em uma linha:

    usar_mtime = origem.preserva_mtime and destino.preserva_mtime

Sem ela, um servidor FTP sem MFMT (que nao consegue gravar a data dos
arquivos enviados) faria TODO arquivo aparecer como diferente, para sempre:
sobe o arquivo, o servidor carimba a data de agora, na proxima comparacao a
data nao bate, sobe de novo. A sincronizacao viraria um moinho que reenvia a
pasta inteira toda vez. Quando algum dos lados nao preserva a data, a
comparacao cai para tamanho - menos precisa, mas honesta.

A tolerancia de 2 segundos existe pelo mesmo tipo de motivo: FAT guarda a
data com resolucao de 2s, e o FTP so tem precisao de minuto na listagem
LIST. Exigir igualdade exata de data seria exigir o impossivel.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

from . import log
from .filters import allowed
from .remotes.base import Cancelado, Entry, ErroRemoto, Remote

logger = log.get()

#: FAT arredonda para 2s; o LIST do FTP so tem minuto. Exigir data exata
#: seria exigir o impossivel.
TOLERANCIA_MTIME = 2.0

# --- estados de um par -----------------------------------------------------
IGUAL = "igual"
SO_ESQUERDA = "so_esquerda"
SO_DIREITA = "so_direita"
ESQ_MAIS_NOVA = "esq_mais_nova"
DIR_MAIS_NOVA = "dir_mais_nova"
TAMANHO_DIFERE = "tamanho_difere"
TIPO_DIFERE = "tipo_difere"

# --- modos de comparacao ---------------------------------------------------
AUTO = "auto"          # data quando os dois lados preservam; senao tamanho
DATA = "data"
TAMANHO = "tamanho"

# --- direcoes de sincronizacao ---------------------------------------------
ENVIAR_NOVOS = "enviar_novos"
BAIXAR_NOVOS = "baixar_novos"
ESPELHAR_ENVIO = "espelhar_envio"
ESPELHAR_BAIXA = "espelhar_baixa"
BIDIRECIONAL = "bidirecional"


@dataclass
class ParComparado:
    """Um arquivo visto dos dois lados."""
    rel: str
    esquerda: Optional[Entry]
    direita: Optional[Entry]
    estado: str
    is_dir: bool = False

    @property
    def nome(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def diferente(self) -> bool:
        return self.estado != IGUAL


def mesmo_arquivo(a: Entry, b: Entry, usar_mtime: bool,
                  tolerancia: float = TOLERANCIA_MTIME) -> bool:
    if a is None or b is None:
        return False
    if a.is_dir != b.is_dir:
        return False
    if a.is_dir:
        return True
    if a.size != b.size:
        return False
    if a.etag and b.etag and a.etag == b.etag:
        return True
    if not usar_mtime:
        return True            # tamanho igual e o que da para afirmar
    if not a.mtime or not b.mtime:
        return True            # sem data dos dois lados, nao da para negar
    return abs(a.mtime - b.mtime) <= tolerancia


class Comparador:
    """Compara duas pastas (local x servidor, ou dois servidores)."""

    def __init__(self, esquerda: Remote, direita: Remote, modo: str = AUTO,
                 incluir: List[str] = None, excluir: List[str] = None,
                 ignorar_case: Optional[bool] = None,
                 tolerancia: float = TOLERANCIA_MTIME):
        self.esq = esquerda
        self.dir = direita
        self.modo = modo
        self.incluir = incluir or []
        self.excluir = excluir or []
        self.tolerancia = tolerancia
        if ignorar_case is None:
            # se QUALQUER lado nao diferencia maiusculas, comparar sem
            # diferenciar: senao "Leiame.txt" e "leiame.txt" virariam dois
            # arquivos e a sincronizacao sobreescreveria um com o outro
            ignorar_case = not (esquerda.case_sensitive and direita.case_sensitive)
        self.ignorar_case = ignorar_case

    @property
    def usar_mtime(self) -> bool:
        if self.modo == DATA:
            return True
        if self.modo == TAMANHO:
            return False
        return bool(self.esq.preserva_mtime and self.dir.preserva_mtime)

    def _chave(self, nome: str) -> str:
        return nome.casefold() if self.ignorar_case else nome

    def _passa_no_filtro(self, rel: str) -> bool:
        return allowed(rel, self.incluir, self.excluir)

    # ------------------------------------------------------------------
    def comparar_pasta(self, cam_esq: str, cam_dir: str,
                       prefixo: str = "") -> List[ParComparado]:
        """Compara UM nivel. E o que pinta os dois paineis."""
        try:
            itens_esq = {self._chave(e.name): e
                         for e in self.esq.listar(cam_esq)}
        except ErroRemoto:
            itens_esq = {}
        try:
            itens_dir = {self._chave(e.name): e
                         for e in self.dir.listar(cam_dir)}
        except ErroRemoto:
            itens_dir = {}

        pares = []
        for chave in sorted(set(itens_esq) | set(itens_dir)):
            a = itens_esq.get(chave)
            b = itens_dir.get(chave)
            nome = (a or b).name
            rel = (prefixo + "/" + nome) if prefixo else nome
            if not self._passa_no_filtro(rel):
                continue
            pares.append(ParComparado(rel=rel, esquerda=a, direita=b,
                                      estado=self._estado(a, b),
                                      is_dir=bool((a or b).is_dir)))
        return pares

    def _estado(self, a: Optional[Entry], b: Optional[Entry]) -> str:
        if a is None:
            return SO_DIREITA
        if b is None:
            return SO_ESQUERDA
        if a.is_dir != b.is_dir:
            return TIPO_DIFERE
        if a.is_dir:
            return IGUAL
        if mesmo_arquivo(a, b, self.usar_mtime, self.tolerancia):
            return IGUAL
        if self.usar_mtime and a.mtime and b.mtime:
            if a.mtime - b.mtime > self.tolerancia:
                return ESQ_MAIS_NOVA
            if b.mtime - a.mtime > self.tolerancia:
                return DIR_MAIS_NOVA
        return TAMANHO_DIFERE

    def mapa_para_painel(self, pares: List[ParComparado]) -> Dict[str, str]:
        """nome -> tag de cor, do jeito que o FilePane espera."""
        return {p.nome: p.estado for p in pares}

    # ------------------------------------------------------------------
    def comparar_arvore(self, cam_esq: str, cam_dir: str,
                        cb: Optional[Callable] = None,
                        cancelar=None) -> Iterator[ParComparado]:
        """Percorre as duas arvores em paralelo, em largura.

        Devolve tambem as pastas (com estado), porque quem sincroniza
        precisa saber que uma pasta so existe de um lado para poder cria-la
        - e, no modo espelho, para poder apaga-la.
        """
        pendentes = [(cam_esq, cam_dir, "")]
        while pendentes:
            if cancelar is not None and cancelar.is_set():
                raise Cancelado()
            a_esq, a_dir, prefixo = pendentes.pop(0)
            if cb is not None:
                cb(prefixo or "/")
            for par in self.comparar_pasta(a_esq, a_dir, prefixo):
                yield par
                if par.is_dir and par.estado != TIPO_DIFERE:
                    nome = (par.esquerda or par.direita).name
                    pendentes.append((self.esq.juntar(a_esq, nome),
                                      self.dir.juntar(a_dir, nome),
                                      par.rel))

    # ------------------------------------------------------------------
    def plano(self, pares: List[ParComparado], direcao: str,
              cam_esq: str, cam_dir: str) -> List[dict]:
        """Traduz a comparacao em acoes.

        Cada acao e um dicionario com: acao ('enviar', 'baixar', 'apagar_esq',
        'apagar_dir', 'criar_pasta_esq', 'criar_pasta_dir'), o par e os dois
        caminhos absolutos. Quem monta os itens de fila e a interface - aqui
        so se decide O QUE fazer, e nao como.
        """
        acoes = []
        for par in pares:
            for acao in self._acoes_do_par(par, direcao, cam_esq, cam_dir):
                acoes.append(acao)
        # pastas antes de arquivos na criacao; arquivos antes de pastas ao
        # apagar - senao se tenta apagar pasta com conteudo dentro
        criar = [a for a in acoes if a["acao"].startswith("criar")]
        transferir = [a for a in acoes if a["acao"] in ("enviar", "baixar")]
        apagar_arq = [a for a in acoes
                      if a["acao"].startswith("apagar") and not a["par"].is_dir]
        apagar_dir = sorted(
            [a for a in acoes
             if a["acao"].startswith("apagar") and a["par"].is_dir],
            key=lambda a: a["par"].rel.count("/"), reverse=True)
        return criar + transferir + apagar_arq + apagar_dir

    def _acoes_do_par(self, par: ParComparado, direcao: str,
                      cam_esq: str, cam_dir: str) -> List[dict]:
        o = self.esq.juntar(cam_esq, par.rel)
        d = self.dir.juntar(cam_dir, par.rel)
        base = {"par": par, "esquerda": o, "direita": d}

        def acao(nome):
            novo = dict(base)
            novo["acao"] = nome
            return [novo]

        if par.estado == IGUAL:
            return []
        if par.estado == TIPO_DIFERE:
            return []       # arquivo de um lado e pasta do outro: nao adivinha

        enviando = direcao in (ENVIAR_NOVOS, ESPELHAR_ENVIO)
        baixando = direcao in (BAIXAR_NOVOS, ESPELHAR_BAIXA)
        espelhando = direcao in (ESPELHAR_ENVIO, ESPELHAR_BAIXA)

        if par.estado == SO_ESQUERDA:
            if enviando or (direcao == BIDIRECIONAL):
                return acao("criar_pasta_dir" if par.is_dir else "enviar")
            if baixando and espelhando:
                return acao("apagar_esq")
            return []

        if par.estado == SO_DIREITA:
            if baixando or (direcao == BIDIRECIONAL):
                return acao("criar_pasta_esq" if par.is_dir else "baixar")
            if enviando and espelhando:
                return acao("apagar_dir")
            return []

        if par.is_dir:
            return []

        # existe dos dois lados e esta diferente
        if enviando:
            return acao("enviar")
        if baixando:
            return acao("baixar")
        if direcao == BIDIRECIONAL:
            if par.estado == ESQ_MAIS_NOVA:
                return acao("enviar")
            if par.estado == DIR_MAIS_NOVA:
                return acao("baixar")
            # mesmo tamanho diferente sem data confiavel: nao ha como decidir
            # qual lado esta certo, e sobrescrever em silencio seria perder
            # trabalho de alguem
            return []
        return []


def resumir(pares: List[ParComparado]) -> Dict[str, int]:
    contagem = {}
    for p in pares:
        contagem[p.estado] = contagem.get(p.estado, 0) + 1
    return contagem


def resumir_plano(acoes: List[dict]) -> str:
    contagem = {}
    for a in acoes:
        contagem[a["acao"]] = contagem.get(a["acao"], 0) + 1
    rotulos = {"enviar": "enviar", "baixar": "baixar",
               "criar_pasta_esq": "criar pasta local",
               "criar_pasta_dir": "criar pasta remota",
               "apagar_esq": "apagar do lado esquerdo",
               "apagar_dir": "apagar do lado direito"}
    partes = ["%d %s" % (n, rotulos.get(k, k)) for k, n in sorted(contagem.items())]
    return ", ".join(partes) if partes else "nada a fazer"
