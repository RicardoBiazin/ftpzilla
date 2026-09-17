"""Busca recursiva num servidor.

O FileZilla tem isso e e uma das funcoes mais usadas: achar aquele arquivo
de configuracao em algum lugar de uma hospedagem com mil pastas.

Duas decisoes que evitam que a busca vire um problema:

1. Ela roda numa conexao SEPARADA, nunca na de navegacao. Uma varredura de
   dez minutos nao pode deixar a pessoa sem conseguir clicar numa pasta.
2. Os resultados sao entregues em lotes, conforme aparecem. Esperar a
   varredura inteira terminar para so entao mostrar algo faz a busca
   parecer travada - e, numa hospedagem grande, ela realmente demora.
"""
from __future__ import annotations

import fnmatch
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import log
from .remotes.base import Cancelado, Entry, ErroRemoto, Remote

logger = log.get()

#: quantos resultados juntar antes de avisar quem esta esperando
LOTE = 25
#: teto de seguranca: link simbolico circular existe, e trava tudo
PROFUNDIDADE_MAXIMA = 12


@dataclass
class Achado:
    caminho: str
    entrada: Entry

    @property
    def nome(self) -> str:
        return self.entrada.name

    @property
    def pasta(self) -> str:
        return self.caminho.rsplit("/", 1)[0] or "/"


class BuscaRemota:
    """Varre uma arvore remota procurando por padrao de nome."""

    def __init__(self, remote: Remote, raiz: str, padrao: str,
                 so_arquivos: bool = False,
                 max_profundidade: int = PROFUNDIDADE_MAXIMA,
                 ignorar_case: bool = True):
        self.remote = remote
        self.raiz = remote.normalizar(raiz)
        self.padrao = padrao or "*"
        if "*" not in self.padrao and "?" not in self.padrao:
            # quem digita "config" quer achar "config.php", e nao so um
            # arquivo chamado exatamente "config"
            self.padrao = "*%s*" % self.padrao
        self.so_arquivos = so_arquivos
        self.max_profundidade = max_profundidade
        self.ignorar_case = ignorar_case
        self.cancelar = threading.Event()
        self.pastas_vistas = 0
        self.achados = 0
        self.terminada = False

    def _casa(self, nome: str) -> bool:
        if self.ignorar_case:
            return fnmatch.fnmatch(nome.casefold(), self.padrao.casefold())
        return fnmatch.fnmatchcase(nome, self.padrao)

    def rodar(self, ao_lote: Optional[Callable] = None,
              ao_pasta: Optional[Callable] = None) -> List[Achado]:
        """Varre em largura. Devolve tudo, e vai avisando por lote."""
        todos: List[Achado] = []
        lote: List[Achado] = []
        pendentes = [(self.raiz, 0)]
        ultimo_aviso = time.time()

        while pendentes:
            if self.cancelar.is_set():
                raise Cancelado()
            caminho, nivel = pendentes.pop(0)
            if ao_pasta is not None:
                ao_pasta(caminho)
            self.pastas_vistas += 1
            try:
                itens = self.remote.listar(caminho)
            except ErroRemoto as e:
                # pasta sem permissao no meio do caminho e comum em
                # hospedagem; anota e segue, em vez de abortar a busca toda
                logger.debug("Busca ignorou %s: %s", caminho, e)
                continue

            for e in itens:
                filho = self.remote.juntar(caminho, e.name)
                if self._casa(e.name) and not (self.so_arquivos and e.is_dir):
                    achado = Achado(filho, e)
                    todos.append(achado)
                    lote.append(achado)
                    self.achados += 1
                if e.is_dir and not e.is_link and nivel < self.max_profundidade:
                    pendentes.append((filho, nivel + 1))

            if lote and (len(lote) >= LOTE or time.time() - ultimo_aviso > 1.0):
                if ao_lote is not None:
                    ao_lote(list(lote))
                lote = []
                ultimo_aviso = time.time()

        if lote and ao_lote is not None:
            ao_lote(lote)
        self.terminada = True
        return todos

    def parar(self) -> None:
        self.cancelar.set()

    def resumo(self) -> str:
        estado = "concluida" if self.terminada else "em andamento"
        return ("%d achado(s) em %d pasta(s) - busca %s"
                % (self.achados, self.pastas_vistas, estado))
