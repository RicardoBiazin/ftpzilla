"""Download segmentado: quando fazer, quando NAO fazer, e se monta certo.

A parte mais importante deste arquivo sao as regras de quando NAO segmentar.
Segmentar sem criterio deixa o programa mais lento que o FileZilla, e ai a
promessa do projeto vira propaganda.
"""
from __future__ import annotations

import hashlib
import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla.fila import GerenciadorFila
from ftpzilla.fila_store import BAIXAR, CONCLUIDO, FilaStore
from ftpzilla.remotes.base import Remote
from ftpzilla.remotes.local import PARCIAL
from ftpzilla.sites import GerenteSites, Site
from ftpzilla.transfer import (LIMIAR_SEGMENTO, MAX_SEGMENTOS,
                               baixar_segmentado, plano_segmentos)

MB = 1024 * 1024


class RemotoFalso(Remote):
    """So capacidades: plano_segmentos nao toca em rede."""

    def __init__(self, segmentavel=True, is_local=False, max_conexoes=4):
        super().__init__(None)
        self.segmentavel = segmentavel
        self.is_local = is_local
        self.max_conexoes = max_conexoes


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado (pip install -r requirements-dev.txt)")


# ---------------------------------------------------------------------------
# As regras
# ---------------------------------------------------------------------------
def teste_quando_segmentar():
    faixas = plano_segmentos(200 * MB, RemotoFalso(), livres=3, esperando=0)
    checar(len(faixas) > 1, "arquivo grande com conexoes livres e segmentado")
    igual(faixas[0][0], 0, "a primeira faixa comeca no zero")
    igual(faixas[-1][1], 200 * MB - 1, "a ultima faixa termina no fim")
    emendam = all(faixas[i][1] + 1 == faixas[i + 1][0]
                  for i in range(len(faixas) - 1))
    checar(emendam, "as faixas emendam sem buraco e sem sobreposicao")
    soma = sum(f - i + 1 for i, f in faixas)
    igual(soma, 200 * MB, "as faixas somam o arquivo inteiro")
    checar(len(faixas) <= MAX_SEGMENTOS, "respeita o maximo de segmentos")


def teste_quando_nao_segmentar():
    """Cada linha aqui e um jeito de a segmentacao piorar o desempenho."""
    igual(plano_segmentos(10 * MB, RemotoFalso(), livres=3), [],
          "arquivo pequeno: o handshake custa mais que o ganho")
    igual(plano_segmentos(200 * MB, RemotoFalso(segmentavel=False), livres=3),
          [], "servidor que nao aceita faixa (sem REST)")
    igual(plano_segmentos(200 * MB, RemotoFalso(is_local=True), livres=3), [],
          "disco local: ler o mesmo disco em paralelo so atrapalha")
    igual(plano_segmentos(200 * MB, RemotoFalso(), livres=3, esperando=5), [],
          "com gente esperando na fila: segmentar rouba conexao de quem "
          "ia transferir outro arquivo")
    igual(plano_segmentos(200 * MB, RemotoFalso(), livres=0), [],
          "sem conexao sobrando no pool")
    igual(plano_segmentos(200 * MB, RemotoFalso(), livres=3,
                          destino_local=False), [],
          "destino que nao e arquivo local comum")
    igual(plano_segmentos(200 * MB, RemotoFalso(max_conexoes=1), livres=3), [],
          "site configurado para uma conexao so")
    igual(plano_segmentos(LIMIAR_SEGMENTO - 1, RemotoFalso(), livres=3), [],
          "um byte abaixo do limiar ainda nao segmenta")


def teste_numero_de_segmentos_respeita_o_pool():
    faixas = plano_segmentos(200 * MB, RemotoFalso(max_conexoes=8), livres=1)
    igual(len(faixas), 2,
          "com uma conexao livre so da para duas faixas (esta mais a nova)")
    faixas = plano_segmentos(80 * MB, RemotoFalso(), livres=8)
    checar(len(faixas) <= 5,
           "faixa minima respeitada: nao adianta picar demais (%d faixas)"
           % len(faixas))


# ---------------------------------------------------------------------------
# Contra o servidor
# ---------------------------------------------------------------------------
def _conteudo(n):
    import random
    r = random.Random(99)
    return bytes(r.getrandbits(8) for _ in range(n))


def _sha(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def teste_monta_o_arquivo_na_ordem_certa():
    """O risco da segmentacao e montar as faixas fora de ordem: o arquivo
    fica com o tamanho certo e o conteudo embaralhado. Por isso o teste
    confere o hash, e nao o tamanho."""
    _precisa()
    from ftpzilla.pool import PoolConexoes

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo(3 * MB)
        escrever(os.path.join(raiz, "grande.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)

        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            pool = PoolConexoes(site, maximo=4)
            try:
                # faixas montadas na mao para nao depender do limiar de 64 MB
                n = 4
                passo = len(dados) // n
                faixas = [(i * passo,
                           (len(dados) - 1) if i == n - 1
                           else (i + 1) * passo - 1) for i in range(n)]

                caminho_alvo = os.path.join(destino,
                                            "grande.bin").replace("\\", "/")

                class ItemFalso:
                    id = 0
                    origem = "/grande.bin"
                    nome = "grande.bin"
                    bytes_feitos = 0

                item = ItemFalso()
                item.destino = caminho_alvo
                from ftpzilla.medidor import Medidor
                medidor = Medidor()
                baixar_segmentado(item, pool, faixas, medidor=medidor)

                alvo = os.path.join(destino, "grande.bin")
                igual(os.path.getsize(alvo), len(dados),
                      "o arquivo montado tem o tamanho certo")
                igual(_sha(alvo), hashlib.sha256(dados).hexdigest(),
                      "e o HASH bate: as quatro faixas foram para o lugar certo")
                checar(not os.path.exists(alvo + PARCIAL),
                       "o parcial virou o arquivo final")
                igual(medidor.feitos, len(dados),
                      "o medidor contou o arquivo inteiro uma vez so")
            finally:
                pool.fechar()


def teste_fila_usa_segmentacao_quando_vale():
    """Com o limiar rebaixado para caber no teste, a fila tem que escolher o
    caminho segmentado sozinha - e o resultado tem que ser identico."""
    _precisa()
    from ftpzilla import transfer

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo(2 * MB)
        escrever(os.path.join(raiz, "g.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)

        limiar, minimo = transfer.LIMIAR_SEGMENTO, transfer.SEGMENTO_MINIMO
        transfer.LIMIAR_SEGMENTO = 512 * 1024
        transfer.SEGMENTO_MINIMO = 256 * 1024
        try:
            with servidores.servidor_ftp(raiz) as (h, p):
                sites = GerenteSites(os.path.join(tmp, "sites.json"))
                site = sites.adicionar(Site(
                    nome="teste", kind="ftp", host=h, porta=p,
                    usuario=servidores.USUARIO, senha=servidores.SENHA,
                    tls_modo="nenhum"))
                g = GerenciadorFila(sites,
                                    store=FilaStore(os.path.join(tmp, "f.db")),
                                    max_global=1).abrir()
                try:
                    d = destino.replace("\\", "/")
                    itens = g.enfileirar(g.montar_itens(site, BAIXAR, [
                        ("/g.bin", d + "/g.bin", len(dados), 0)]))
                    checar(g.esperar_vazia(120), "a fila terminou")
                    igual(itens[0].estado, CONCLUIDO, "concluiu")
                    alvo = os.path.join(destino, "g.bin")
                    igual(_sha(alvo), hashlib.sha256(dados).hexdigest(),
                          "o arquivo baixado em paralelo tem o hash certo")
                    igual(len(g.store.segmentos(itens[0].id)), 0,
                          "as faixas foram limpas do banco ao terminar")
                finally:
                    g.fechar()
        finally:
            transfer.LIMIAR_SEGMENTO = limiar
            transfer.SEGMENTO_MINIMO = minimo


def teste_retoma_segmentos():
    """Fechar o programa no meio de um download segmentado nao pode obrigar
    as quatro faixas a recomecar."""
    _precisa()
    from ftpzilla.pool import PoolConexoes

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo(2 * MB)
        escrever(os.path.join(raiz, "g.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)
        alvo = os.path.join(destino, "g.bin")

        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            pool = PoolConexoes(site, maximo=4)
            try:
                n = 2
                passo = len(dados) // n
                faixas = [(0, passo - 1), (passo, len(dados) - 1)]

                # simula uma sessao anterior: metade da primeira faixa pronta
                ja = passo // 2
                with open(alvo + PARCIAL, "wb") as f:
                    f.truncate(len(dados))
                    f.seek(0)
                    f.write(dados[:ja])

                class ItemFalso:
                    id = 0
                    origem = "/g.bin"
                    destino = alvo.replace("\\", "/")
                    nome = "g.bin"
                    bytes_feitos = ja

                baixar_segmentado(ItemFalso(), pool, faixas, feitos=[ja, 0])
                igual(os.path.getsize(alvo), len(dados), "tamanho certo")
                igual(_sha(alvo), hashlib.sha256(dados).hexdigest(),
                      "hash certo: o pedaco ja baixado foi mantido e o resto "
                      "caiu exatamente depois dele")
            finally:
                pool.fechar()


TESTES = [teste_quando_segmentar, teste_quando_nao_segmentar,
          teste_numero_de_segmentos_respeita_o_pool,
          teste_monta_o_arquivo_na_ordem_certa,
          teste_fila_usa_segmentacao_quando_vale, teste_retoma_segmentos]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Download segmentado"))
