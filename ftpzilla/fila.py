"""A fila de transferencias: quem decide o que roda, quando e em que ordem.

A fila e GLOBAL, e nao por aba. Trocar de aba, ou ate fechar a aba de um
servidor, nao pode jogar fora uma transferencia em andamento.

A ordem nao e uma queue.Queue - e uma lista ordenada por um numero de
prioridade FRACIONARIO. Subir um item vira "prioridade = media entre o de
cima e o de baixo", entao reordenar custa uma linha, e nao renumerar cinco
mil. Esse detalhe e o que permite arrastar itens numa fila grande sem a
interface travar.

Estados e transicoes:

    esperando -> rodando -> concluido
                        \\-> pausado   (usuario pediu, ou fechou o programa)
                        \\-> esperando (erro transitorio, com espera)
                        \\-> falhou    (erro permanente, ou tentativas demais)
                        \\-> cancelado (usuario desistiu)
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import log, transfer
from .fila_store import (ATIVOS, BAIXAR, CANCELADO, CONCLUIDO, ENCERRADOS,
                         ENVIAR, ESPERANDO, FALHOU, PAUSADO, PULADO, RODANDO,
                         FilaStore)
from .medidor import Limitador, Medidor, MedidorGlobal
from .pool import PoolConexoes, PoolLocal
from .remotes.base import Cancelado, ErroRemoto
from .remotes.local import LocalRemote

logger = log.get()

#: transferencias simultaneas somando todos os servidores
MAX_GLOBAL = 6


@dataclass
class ItemFila:
    """Uma transferencia. Espelha uma linha da tabela 'itens'."""
    id: int = 0
    grupo: str = ""
    site_id: str = ""
    site_nome: str = ""
    sentido: str = BAIXAR
    origem: str = ""
    destino: str = ""
    tamanho: int = -1
    mtime: float = 0.0
    bytes_feitos: int = 0
    estado: str = ESPERANDO
    prioridade: float = 0.0
    tentativas: int = 0
    proxima_em: float = 0.0
    erro: str = ""
    acao_existente: str = "perguntar"
    upload_url: str = ""
    criado_em: float = 0.0
    atualizado_em: float = 0.0

    # --- so em memoria ----------------------------------------------------
    medidor: Optional[Medidor] = field(default=None, repr=False, compare=False)
    cancelar: Optional[threading.Event] = field(default=None, repr=False,
                                                compare=False)
    offset_inicial: int = field(default=0, repr=False, compare=False)
    #: quanto ja estava feito na falha anterior - ver a regra de tentativas
    marca_ultima_falha: int = field(default=0, repr=False, compare=False)
    #: a tentativa atual usou varias conexoes?
    segmentado: bool = field(default=False, repr=False, compare=False)

    @property
    def nome(self) -> str:
        base = self.origem.replace("\\", "/").rstrip("/")
        return base.rsplit("/", 1)[-1] or base

    @property
    def ativo(self) -> bool:
        return self.estado in ATIVOS

    def como_dict(self) -> dict:
        from .fila_store import COLUNAS
        d = {}
        for c in COLUNAS:
            if c == "id":
                continue
            d[c] = getattr(self, c)
        return d

    def snapshot(self) -> dict:
        """O que a interface precisa para desenhar a linha."""
        if self.medidor is not None and self.estado == RODANDO:
            s = self.medidor.snapshot()
            feitos = self.offset_inicial + s["feitos"]
            total = self.tamanho if self.tamanho >= 0 else s["total"]
            pct = (feitos / total * 100.0) if total > 0 else 0.0
            return {"feitos": feitos, "total": total, "pct": min(pct, 100.0),
                    "velocidade": s["velocidade"], "eta": s["eta"]}
        total = max(self.tamanho, 0)
        pct = (self.bytes_feitos / total * 100.0) if total else 0.0
        if self.estado == CONCLUIDO:
            pct = 100.0
        return {"feitos": self.bytes_feitos, "total": total,
                "pct": min(pct, 100.0), "velocidade": 0.0, "eta": -1.0}


class GerenciadorFila:
    """Dona da fila: agenda, executa, persiste e avisa a interface."""

    def __init__(self, gerente_sites, store: Optional[FilaStore] = None,
                 max_global: int = MAX_GLOBAL, ao_mudar: Optional[Callable] = None):
        self.gerente_sites = gerente_sites
        self.store = store or FilaStore()
        self.max_global = max_global
        self.ao_mudar = ao_mudar          # avisa a interface (thread da fila!)
        self.global_medidor = MedidorGlobal()
        self.limitador = Limitador(0)

        self.itens: List[ItemFila] = []
        self._por_id: Dict[int, ItemFila] = {}
        self._lock = threading.RLock()
        self._pools: Dict[str, object] = {}
        # sites que existem so nesta sessao (conexao rapida, ou um site
        # aberto e ainda nao salvo): sem isto, transferir de uma conexao
        # rapida falharia com "o site nao existe mais", que e verdade do
        # ponto de vista do arquivo e mentira do ponto de vista do usuario
        self._sites_sessao: Dict[str, object] = {}
        self._pool_local = PoolLocal()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._thread: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._acordar = threading.Event()
        self._pausada = False
        self._sites_pausados = set()
        # sites que ja mostraram nao aguentar conexao simultanea: o servidor
        # derruba a segunda. Descoberto na pratica, nao configurado.
        self._sem_paralelismo = set()
        self._rodando = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def abrir(self, retomar: bool = False) -> "GerenciadorFila":
        self.store.abrir()
        for d in self.store.carregar():
            item = self._do_banco(d)
            self.itens.append(item)
            self._por_id[item.id] = item
        self._ordenar()
        self._recalcular_totais()
        self._executor = ThreadPoolExecutor(max_workers=max(1, self.max_global),
                                            thread_name_prefix="transfere")
        self._thread = threading.Thread(target=self._agendador,
                                        name="fila-agendador", daemon=True)
        self._thread.start()
        pendentes = [i for i in self.itens if i.estado == PAUSADO]
        if pendentes:
            logger.info("%d transferencia(s) da sessao anterior estao na fila, "
                        "pausadas.", len(pendentes))
            if retomar:
                self.retomar([i.id for i in pendentes])
        return self

    @staticmethod
    def _do_banco(d: dict) -> ItemFila:
        campos = {k: v for k, v in d.items()
                  if k in ItemFila.__dataclass_fields__}
        return ItemFila(**campos)

    def fechar(self) -> None:
        self._parar.set()
        self._acordar.set()
        with self._lock:
            for item in self.itens:
                if item.cancelar is not None:
                    item.cancelar.set()
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        for pool in list(self._pools.values()):
            pool.fechar()
        self._pools.clear()
        self.store.fechar()

    # ------------------------------------------------------------------
    # Entrada de itens
    # ------------------------------------------------------------------
    def enfileirar(self, itens: List[ItemFila]) -> List[ItemFila]:
        """Grava e agenda. Devolve os itens ja com id."""
        if not itens:
            return []
        with self._lock:
            base = max([i.prioridade for i in self.itens], default=0.0)
            for n, item in enumerate(itens, 1):
                item.prioridade = base + n
                item.criado_em = item.criado_em or time.time()
            ids = self.store.inserir([i.como_dict() for i in itens])
            for item, novo_id in zip(itens, ids):
                item.id = novo_id
                self.itens.append(item)
                self._por_id[novo_id] = item
            self._recalcular_totais()
        self._avisar()
        self._acordar.set()
        return itens

    def montar_itens(self, site, sentido: str, pares, acao: str = "sobrescrever",
                     grupo: str = "") -> List[ItemFila]:
        """Monta itens a partir de pares (origem, destino, tamanho, mtime)."""
        grupo = grupo or ("g%d" % int(time.time() * 1000))
        out = []
        for origem, destino, tamanho, mtime in pares:
            out.append(ItemFila(
                grupo=grupo,
                site_id=getattr(site, "id", "") if site else "",
                site_nome=getattr(site, "nome", "") if site else "",
                sentido=sentido, origem=origem, destino=destino,
                tamanho=int(tamanho if tamanho is not None else -1),
                mtime=float(mtime or 0.0), acao_existente=acao))
        return out

    def expandir(self, remote, sentido: str, caminhos, pasta_destino: str,
                 destino_remote=None, cancelar=None):
        """Percorre pastas e devolve a lista de pares de arquivo.

        Roda numa thread de trabalho (nao na do Tkinter): uma pasta remota
        com muitos niveis leva tempo, e a janela nao pode congelar enquanto
        isso acontece.
        """
        destino_remote = destino_remote or LocalRemote()
        pares = []

        def descer(origem_dir: str, destino_dir: str) -> None:
            for e in remote.listar(origem_dir):
                if cancelar is not None and cancelar.is_set():
                    raise Cancelado()
                o = remote.juntar(origem_dir, e.name)
                d = destino_remote.juntar(destino_dir, e.name)
                if e.is_dir and not e.is_link:
                    descer(o, d)
                else:
                    pares.append((o, d, e.size, e.mtime))

        for caminho in caminhos:
            info = remote.stat(caminho)
            nome = remote.nome(caminho)
            alvo = destino_remote.juntar(pasta_destino, nome)
            if info is not None and info.is_dir:
                descer(caminho, alvo)
            else:
                pares.append((caminho, alvo,
                              info.size if info else -1,
                              info.mtime if info else 0.0))
        return pares

    # ------------------------------------------------------------------
    # Controle
    # ------------------------------------------------------------------
    def pausar_tudo(self, tambem_em_curso: bool = False) -> None:
        with self._lock:
            self._pausada = True
            if tambem_em_curso:
                for item in self.itens:
                    if item.estado == RODANDO and item.cancelar is not None:
                        item.cancelar.set()
        self._avisar()

    def retomar_tudo(self) -> None:
        with self._lock:
            self._pausada = False
            self._sites_pausados.clear()
            for item in self.itens:
                if item.estado in (PAUSADO, FALHOU):
                    self._mudar(item, ESPERANDO, tentativas=0, erro="",
                                proxima_em=0.0)
        self._acordar.set()
        self._avisar()

    def pausar(self, ids: List[int]) -> None:
        with self._lock:
            for i in ids:
                item = self._por_id.get(i)
                if item is None:
                    continue
                if item.estado == RODANDO and item.cancelar is not None:
                    item.cancelar.set()     # o worker grava o PAUSADO
                elif item.estado == ESPERANDO:
                    self._mudar(item, PAUSADO)
        self._avisar()

    def retomar(self, ids: List[int]) -> None:
        with self._lock:
            for i in ids:
                item = self._por_id.get(i)
                if item is not None and item.estado in (PAUSADO, FALHOU,
                                                        CANCELADO):
                    self._mudar(item, ESPERANDO, tentativas=0, erro="",
                                proxima_em=0.0)
        self._acordar.set()
        self._avisar()

    def cancelar(self, ids: List[int], apagar_parcial: bool = False) -> None:
        with self._lock:
            for i in ids:
                item = self._por_id.get(i)
                if item is None:
                    continue
                if item.cancelar is not None:
                    item.cancelar.set()
                item.estado = CANCELADO
                self.store.atualizar(item.id, estado=CANCELADO)
                self.global_medidor.desistir(item.id)
                if apagar_parcial and item.sentido == BAIXAR:
                    self._apagar_parcial(item)
        self._avisar()

    @staticmethod
    def _apagar_parcial(item: ItemFila) -> None:
        from .remotes.local import PARCIAL
        try:
            os.remove(LocalRemote().nativo(item.destino) + PARCIAL)
        except OSError:
            pass

    def remover(self, ids: List[int]) -> None:
        self.cancelar(ids)
        with self._lock:
            for i in ids:
                item = self._por_id.pop(i, None)
                if item is not None and item in self.itens:
                    self.itens.remove(item)
            self.store.remover(list(ids))
            self._recalcular_totais()
        self._avisar()

    def limpar_encerrados(self) -> int:
        with self._lock:
            restantes = [i for i in self.itens if i.estado not in ENCERRADOS]
            n = len(self.itens) - len(restantes)
            self.itens = restantes
            self._por_id = {i.id: i for i in restantes}
            self.store.limpar()
            self.global_medidor.zerar()
            self._recalcular_totais()
        self._avisar()
        return n

    def tentar_de_novo(self, ids: List[int] = None) -> None:
        """Zera o contador das falhas e devolve os itens para a fila."""
        with self._lock:
            alvos = ([self._por_id[i] for i in ids if i in self._por_id]
                     if ids else [i for i in self.itens if i.estado == FALHOU])
            for item in alvos:
                self._mudar(item, ESPERANDO, tentativas=0, erro="",
                            proxima_em=0.0)
        self._acordar.set()
        self._avisar()

    def mover(self, ids: List[int], para_cima: bool = True) -> None:
        """Reordena por prioridade fracionaria: nao renumera a fila toda."""
        with self._lock:
            self._ordenar()
            for item_id in (ids if para_cima else reversed(ids)):
                item = self._por_id.get(item_id)
                if item is None:
                    continue
                pos = self.itens.index(item)
                alvo = pos - 1 if para_cima else pos + 1
                if alvo < 0 or alvo >= len(self.itens):
                    continue
                vizinho = self.itens[alvo]
                if para_cima:
                    anterior = (self.itens[alvo - 1].prioridade
                                if alvo - 1 >= 0 else vizinho.prioridade - 2.0)
                    nova = (anterior + vizinho.prioridade) / 2.0
                else:
                    seguinte = (self.itens[alvo + 1].prioridade
                                if alvo + 1 < len(self.itens)
                                else vizinho.prioridade + 2.0)
                    nova = (vizinho.prioridade + seguinte) / 2.0
                item.prioridade = nova
                self.store.atualizar(item.id, prioridade=nova)
                self._ordenar()
        self._avisar()

    def definir_limite(self, kbs: int) -> None:
        self.limitador.definir(int(kbs or 0) * 1024)

    # ------------------------------------------------------------------
    # Agendamento
    # ------------------------------------------------------------------
    def _agendador(self) -> None:
        while not self._parar.is_set():
            proximo = self._despachar()
            self._acordar.wait(timeout=proximo)
            self._acordar.clear()

    def _despachar(self) -> float:
        """Manda para o executor tudo que puder rodar agora.

        Devolve em quantos segundos vale a pena olhar de novo (o menor
        tempo de espera de algum item em backoff), para nao ficar acordando
        a toa com a fila parada.
        """
        proximo = 5.0
        with self._lock:
            if self._pausada:
                return proximo
            agora = time.time()
            for item in self.itens:
                if self._rodando >= self.max_global:
                    break
                if item.estado != ESPERANDO:
                    continue
                if item.site_id in self._sites_pausados:
                    continue
                if item.proxima_em > agora:
                    proximo = min(proximo, item.proxima_em - agora)
                    continue
                pool = self._pool_do(item)
                if pool is None:
                    self._mudar(item, FALHOU,
                                erro="O site deste item nao existe mais.")
                    continue
                if pool is not self._pool_local and pool.livres <= 0:
                    continue
                item.cancelar = threading.Event()
                item.medidor = Medidor()
                self._mudar(item, RODANDO, erro="")
                self.global_medidor.registrar(item.id, item.medidor)
                self._rodando += 1
                self._executor.submit(self._rodar, item)
        return proximo

    def registrar_site(self, site) -> None:
        """Torna um site alcancavel pela fila mesmo sem estar salvo."""
        if site is not None and getattr(site, "id", ""):
            self._sites_sessao[site.id] = site

    def _site(self, site_id: str):
        site = self._sites_sessao.get(site_id)
        if site is not None:
            return site
        return (self.gerente_sites.por_id(site_id)
                if self.gerente_sites is not None else None)

    def _pool_do(self, item: ItemFila):
        if not item.site_id:
            return self._pool_local
        pool = self._pools.get(item.site_id)
        if pool is not None:
            return pool
        site = self._site(item.site_id)
        if site is None:
            return None
        pool = PoolConexoes(site)
        self._pools[item.site_id] = pool
        return pool

    # ------------------------------------------------------------------
    def _rodar(self, item: ItemFila) -> None:
        pool = self._pool_do(item)
        remoto = None
        suja = False
        item.segmentado = False
        try:
            remoto = pool.pegar()
            plano = self._plano_segmentado(item, pool, remoto)
            if plano:
                # devolve esta conexao ANTES de comecar: as faixas pegam as
                # suas do pool, e devolver aqui evita que a mesma conexao
                # seja devolvida duas vezes se algo falhar la dentro
                pool.devolver(remoto)
                remoto = None
                faixas, feitos, paralelas = plano
                item.segmentado = paralelas > 1
                if paralelas > 1:
                    logger.info("Baixando %s em %d conexoes paralelas.",
                                item.nome, len(faixas))
                else:
                    logger.info("Continuando %s numa conexao so, em %d "
                                "faixas.", item.nome, len(faixas))
                transfer.baixar_segmentado(
                    item, pool, faixas, medidor=item.medidor,
                    cancelar=item.cancelar, store=self.store,
                    limitador=self.limitador, feitos=feitos,
                    paralelas=paralelas)
                self._terminou(item)
                return
            local = LocalRemote()
            origem, destino = ((remoto, local) if item.sentido == BAIXAR
                               else (local, remoto))
            transfer.executar(item, origem, destino, medidor=item.medidor,
                              cancelar=item.cancelar, store=self.store,
                              limitador=self.limitador)
            self._terminou(item)
        except Cancelado:
            suja = True
            self._pausou(item)
        except BaseException as e:      # noqa: BLE001 - tudo vira estado
            suja = True
            self._falhou(item, e)
        finally:
            if remoto is not None:
                pool.devolver(remoto, suja=suja)
            with self._lock:
                self._rodando = max(0, self._rodando - 1)
            item.cancelar = None
            self._acordar.set()
            self._avisar()

    def _plano_segmentado(self, item: ItemFila, pool, remoto):
        """(faixas, feitos) quando vale baixar em paralelo; None quando nao.

        A decisao precisa da conexao ja aberta: so depois do FEAT se sabe se
        o servidor aceita faixa.
        """
        if item.sentido != BAIXAR or pool is self._pool_local:
            return None
        tamanho = item.tamanho
        if tamanho < 0:
            info = remoto.stat(item.origem)
            tamanho = info.size if info else -1
            item.tamanho = tamanho
        with self._lock:
            esperando = sum(1 for i in self.itens if i.estado == ESPERANDO)
        if item.site_id in self._sem_paralelismo:
            # o servidor ja derrubou conexao simultanea antes. Se houver
            # segmentos do que ja foi baixado, continuar de onde parou - com
            # UMA conexao de cada vez; senao, nem segmentar.
            gravados = self.store.segmentos(item.id)
            if not gravados:
                return None
            faixas = [(g["ini"], g["fim"]) for g in gravados]
            feitos = self._segmentos_reaproveitaveis(item, faixas, tamanho)
            if feitos is None:
                return None
            return faixas, feitos, 1

        faixas = transfer.plano_segmentos(tamanho, remoto,
                                          livres=pool.livres,
                                          esperando=esperando)
        if not faixas:
            return None

        feitos = self._segmentos_reaproveitaveis(item, faixas, tamanho)
        if feitos is None:
            self.store.gravar_segmentos(item.id, faixas)
            feitos = [0] * len(faixas)
        return faixas, feitos, len(faixas)

    def _segmentos_reaproveitaveis(self, item: ItemFila, faixas, tamanho: int):
        """O que ja foi baixado de cada faixa numa tentativa anterior.

        Devolve None quando nao da para confiar no que esta no disco - o
        parcial tem que existir, ter exatamente o tamanho final (ele e
        pre-alocado) e as faixas gravadas tem que ser as mesmas de agora.
        """
        from .remotes.local import PARCIAL
        parcial = LocalRemote().nativo(item.destino) + PARCIAL
        if not os.path.exists(parcial) or os.path.getsize(parcial) != tamanho:
            return None
        gravados = self.store.segmentos(item.id)
        if len(gravados) != len(faixas):
            return None
        for g, (ini, fim) in zip(gravados, faixas):
            if g["ini"] != ini or g["fim"] != fim:
                return None
            if not (0 <= g["feito"] <= fim - ini + 1):
                return None
        return [g["feito"] for g in gravados]

    def _terminou(self, item: ItemFila) -> None:
        with self._lock:
            item.bytes_feitos = item.offset_inicial + (
                item.medidor.feitos if item.medidor else 0)
            if item.tamanho < 0:
                item.tamanho = item.bytes_feitos
            self._mudar(item, CONCLUIDO, bytes_feitos=item.bytes_feitos,
                        tamanho=item.tamanho, erro="")
            self.global_medidor.concluir(item.id, item.bytes_feitos)
        logger.info("Concluido: %s (%s)", item.nome,
                    "enviado" if item.sentido == ENVIAR else "baixado")

    def _pausou(self, item: ItemFila) -> None:
        with self._lock:
            self.global_medidor.desistir(item.id)
            estado = CANCELADO if item.estado == CANCELADO else PAUSADO
            self._mudar(item, estado, bytes_feitos=item.bytes_feitos)

    def _falhou(self, item: ItemFila, exc: BaseException) -> None:
        tipo = transfer.classificar(exc)
        mensagem = transfer.descrever_erro(exc)
        with self._lock:
            self.global_medidor.desistir(item.id)
            # "andou nesta tentativa" nao basta: com o download em faixas,
            # uma faixa avanca alguns bytes e outra morre SEMPRE no mesmo
            # ponto, o contador zerava toda vez e o item repetia para sempre
            # sem nunca passar de onde estava. O que vale e ter avancado em
            # relacao a ULTIMA falha.
            progrediu = item.bytes_feitos > item.marca_ultima_falha
            item.marca_ultima_falha = max(item.marca_ultima_falha,
                                          item.bytes_feitos)
            if progrediu:
                # um arquivo grande em link instavel cai varias vezes e ainda
                # assim chega ao fim; nao pode morrer por contagem
                item.tentativas = 0

            if tipo == transfer.AUTENTICACAO:
                self._sites_pausados.add(item.site_id)
                self._mudar(item, PAUSADO, erro=mensagem,
                            bytes_feitos=item.bytes_feitos)
                logger.error("Credencial recusada em %s: a fila deste site foi "
                             "pausada para nao trancar a conta.",
                             item.site_nome or item.site_id)
            elif tipo == transfer.CONFIANCA:
                self._sites_pausados.add(item.site_id)
                self._mudar(item, PAUSADO, erro=mensagem,
                            bytes_feitos=item.bytes_feitos)
                logger.error("%s", mensagem)
            elif tipo == transfer.TRANSITORIO and \
                    item.tentativas + 1 <= transfer.MAX_TENTATIVAS:
                item.tentativas += 1
                atraso = transfer.espera(item.tentativas)
                self._mudar(item, ESPERANDO, erro=mensagem,
                            tentativas=item.tentativas,
                            proxima_em=time.time() + atraso,
                            bytes_feitos=item.bytes_feitos)
                logger.warning("%s: %s. Tentativa %d de %d em %.0fs.",
                               item.nome, mensagem, item.tentativas,
                               transfer.MAX_TENTATIVAS, atraso)
            else:
                self._mudar(item, FALHOU, erro=mensagem,
                            bytes_feitos=item.bytes_feitos)
                logger.error("%s: %s", item.nome, mensagem)

            if isinstance(exc, ErroRemoto) and "421" in str(exc):
                pool = self._pools.get(item.site_id)
                if pool is not None:
                    pool.reduzir_teto("421 do servidor")

            if item.segmentado and tipo == transfer.TRANSITORIO and \
                    item.site_id not in self._sem_paralelismo:
                # a otimizacao nunca pode impedir a transferencia: se o
                # servidor derruba conexao simultanea, desiste do paralelo
                # para este site e continua com uma conexao so, aproveitando
                # o que ja foi baixado
                self._sem_paralelismo.add(item.site_id)
                pool = self._pools.get(item.site_id)
                if pool is not None:
                    pool.limitar(1)
                    # as conexoes abertas na tentativa paralela continuariam
                    # guardadas e voltariam a ser usadas duas a duas na
                    # proxima faixa; fechar agora e o que faz o recuo valer
                    # ja na tentativa seguinte
                    pool.fechar_ociosas(0)
                logger.warning(
                    "%s derrubou as conexoes simultaneas. Continuando com "
                    "uma conexao so, a partir do que ja foi baixado.",
                    item.site_nome or item.site_id)

    # ------------------------------------------------------------------
    # Apoio
    # ------------------------------------------------------------------
    def _mudar(self, item: ItemFila, estado: str, **campos) -> None:
        item.estado = estado
        for k, v in campos.items():
            setattr(item, k, v)
        self.store.atualizar(item.id, estado=estado, **campos)

    def _ordenar(self) -> None:
        self.itens.sort(key=lambda i: (i.prioridade, i.id))

    def _recalcular_totais(self) -> None:
        ativos = [i for i in self.itens if i.estado not in (CANCELADO, PULADO)]
        total = sum(max(i.tamanho, 0) for i in ativos)
        self.global_medidor.definir_totais(len(ativos), total)

    def _avisar(self) -> None:
        if self.ao_mudar is not None:
            try:
                self.ao_mudar()
            except Exception:       # noqa: BLE001 - a interface nao derruba a fila
                pass

    # ------------------------------------------------------------------
    def resumo(self) -> dict:
        with self._lock:
            contagem = {}
            for item in self.itens:
                contagem[item.estado] = contagem.get(item.estado, 0) + 1
            s = self.global_medidor.snapshot()
            s["contagem"] = contagem
            s["pausada"] = self._pausada
            s["rodando"] = self._rodando
            return s

    def por_estado(self, *estados) -> List[ItemFila]:
        with self._lock:
            return [i for i in self.itens if i.estado in estados]

    def esperar_vazia(self, timeout: float = 60.0) -> bool:
        """So para testes e para fechar o programa: espera a fila esvaziar."""
        fim = time.time() + timeout
        while time.time() < fim:
            with self._lock:
                pendentes = [i for i in self.itens
                             if i.estado in (ESPERANDO, RODANDO)]
            if not pendentes:
                return True
            time.sleep(0.05)
        return False
