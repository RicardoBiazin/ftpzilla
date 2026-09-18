"""Descobrir o que ja existe no destino, antes de transferir.

Sobrescrever em silencio e a forma mais facil de um cliente de transferencia
destruir trabalho: a pessoa arrasta a pasta de novo "para pegar o que
faltava" e perde o que havia editado do outro lado.

A consulta e feita por PASTA, e nao por arquivo: uma listagem devolve nome,
tamanho e data de tudo de uma vez, enquanto um stat por arquivo custaria uma
viagem ate o servidor para cada linha - com trezentos arquivos isso e a
diferenca entre meio segundo e varios minutos.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import log
from .fila_store import BAIXAR
from .remotes.base import Entry, ErroRemoto

logger = log.get()

# --- o que fazer quando o arquivo ja existe --------------------------------
PERGUNTAR = "perguntar"
SOBRESCREVER = "sobrescrever"
SE_MAIS_NOVO = "se_mais_novo"
RETOMAR = "resumir"
RENOMEAR = "renomear"
PULAR = "pular"

ROTULOS = [
    (SOBRESCREVER, "Substituir o arquivo do destino"),
    (SE_MAIS_NOVO, "Substituir so se a origem for mais nova"),
    (RETOMAR, "Continuar de onde parou (o destino e um arquivo incompleto)"),
    (RENOMEAR, "Manter os dois: renomear o que esta chegando"),
    (PULAR, "Nao transferir este arquivo"),
]


@dataclass
class Conflito:
    """Um arquivo que ja existe no destino."""
    indice: int                 # posicao do par na lista original
    origem: str
    destino: str
    tamanho_origem: int
    mtime_origem: float
    tamanho_destino: int
    mtime_destino: float

    @property
    def nome(self) -> str:
        return self.destino.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def origem_mais_nova(self) -> bool:
        if not self.mtime_origem or not self.mtime_destino:
            return False
        return self.mtime_origem > self.mtime_destino + 2.0

    @property
    def iguais(self) -> bool:
        """Mesmo tamanho e mesma data: quase certamente o mesmo arquivo."""
        if self.tamanho_origem != self.tamanho_destino:
            return False
        if not self.mtime_origem or not self.mtime_destino:
            return True
        return abs(self.mtime_origem - self.mtime_destino) <= 2.0

    @property
    def destino_incompleto(self) -> bool:
        """Cabe oferecer 'continuar de onde parou'?"""
        return 0 < self.tamanho_destino < self.tamanho_origem


def _pasta(caminho: str) -> str:
    caminho = caminho.replace("\\", "/")
    return caminho.rsplit("/", 1)[0] if "/" in caminho else "/"


def _nome(caminho: str) -> str:
    return caminho.replace("\\", "/").rsplit("/", 1)[-1]


def detectar(pares, sentido: str, remoto=None) -> List[Conflito]:
    """Quais dos pares (origem, destino, tamanho, mtime) ja existem la.

    'remoto' so e usado no envio, para listar as pastas de destino do
    servidor. No download o destino e o disco, e a consulta e local.
    """
    por_pasta: Dict[str, List[tuple]] = {}
    for i, (origem, destino, tamanho, mtime) in enumerate(pares):
        por_pasta.setdefault(_pasta(destino), []).append(
            (i, origem, destino, tamanho, mtime))

    conflitos = []
    for pasta, itens in por_pasta.items():
        existentes = _listar(pasta, sentido, remoto)
        if not existentes:
            continue
        for i, origem, destino, tamanho, mtime in itens:
            atual = existentes.get(_nome(destino))
            if atual is None:
                continue
            conflitos.append(Conflito(
                indice=i, origem=origem, destino=destino,
                tamanho_origem=int(tamanho if tamanho is not None else -1),
                mtime_origem=float(mtime or 0.0),
                tamanho_destino=atual.size, mtime_destino=atual.mtime))
    return conflitos


def _listar(pasta: str, sentido: str, remoto) -> Dict[str, Entry]:
    """Nome -> Entry do que ja esta na pasta de destino."""
    if sentido == BAIXAR:
        nativo = pasta.replace("/", os.sep)
        saida = {}
        try:
            with os.scandir(nativo) as it:
                for de in it:
                    try:
                        info = de.stat()
                        saida[de.name] = Entry(name=de.name,
                                               is_dir=de.is_dir(),
                                               size=info.st_size,
                                               mtime=float(info.st_mtime))
                    except OSError:
                        continue
        except OSError:
            return {}
        return saida

    if remoto is None:
        return {}
    try:
        return {e.name: e for e in remoto.listar(pasta)}
    except ErroRemoto as e:
        logger.info("Nao deu para conferir o que ja existe em %s: %s", pasta, e)
        return {}


def nome_livre(destino: str, existentes) -> str:
    """'a.txt' -> 'a (2).txt', pulando os que ja estao ocupados."""
    caminho = destino.replace("\\", "/")
    pasta, nome = (caminho.rsplit("/", 1) if "/" in caminho
                   else ("", caminho))
    base, ponto, ext = nome.rpartition(".")
    if not ponto:
        base, ext = nome, ""
    n = 2
    while True:
        novo = "%s (%d)%s%s" % (base, n, "." if ext else "", ext)
        if novo not in existentes:
            return ("%s/%s" % (pasta, novo)) if pasta else novo
        n += 1


def aplicar(pares, conflitos: List[Conflito], decisoes: Dict[int, str],
            existentes_por_pasta: Optional[Dict[str, set]] = None):
    """Devolve (pares_para_enfileirar, acoes, pulados).

    'acoes' acompanha cada par que sobrou, para a transferencia saber se e
    para substituir ou continuar de onde parou. Renomear acontece aqui
    mesmo: o par ja sai com o destino novo.
    """
    existentes_por_pasta = existentes_por_pasta or {}
    por_indice = {c.indice: c for c in conflitos}
    saida, acoes, pulados = [], [], 0

    for i, par in enumerate(pares):
        conflito = por_indice.get(i)
        if conflito is None:
            saida.append(par)
            acoes.append(SOBRESCREVER)
            continue

        decisao = decisoes.get(i, SOBRESCREVER)
        if decisao == SE_MAIS_NOVO:
            decisao = SOBRESCREVER if conflito.origem_mais_nova else PULAR
        if decisao == PULAR:
            pulados += 1
            continue
        if decisao == RENOMEAR:
            pasta = _pasta(par[1])
            ocupados = existentes_por_pasta.setdefault(pasta, set())
            ocupados.add(_nome(par[1]))
            novo = nome_livre(par[1], ocupados)
            ocupados.add(_nome(novo))
            saida.append((par[0], novo, par[2], par[3]))
            acoes.append(SOBRESCREVER)
            continue

        saida.append(par)
        acoes.append(decisao)

    return saida, acoes, pulados
