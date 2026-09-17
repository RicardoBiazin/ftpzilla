"""Retomada: o teste que separa um cliente confiavel de um perigoso.

Nao basta o arquivo final ter o tamanho certo - um arquivo montado de dois
pedacos errados tem o tamanho certo tambem. Por isso aqui se confere o HASH
do que chegou, sempre.
"""
from __future__ import annotations

import hashlib
import io
import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla.fila import GerenciadorFila
from ftpzilla.fila_store import BAIXAR, CONCLUIDO, ENVIAR, FilaStore
from ftpzilla.remotes.ftp import FtpRemote
from ftpzilla.remotes.local import PARCIAL
from ftpzilla.sites import GerenteSites, Site
from ftpzilla.transfer import (MAX_TENTATIVAS, Retomada, classificar,
                               validar_download, validar_upload)

TAM = 900 * 1024
CORTE = 250 * 1024


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado (pip install -r requirements-dev.txt)")


def _conteudo(n=TAM) -> bytes:
    # conteudo variado de proposito: um arquivo de 'aaaa' esconderia bytes
    # trocados de lugar, que e exatamente o defeito que este teste procura
    import random
    r = random.Random(1234)
    return bytes(r.getrandbits(8) for _ in range(n))


def _sha(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(65536), b""):
            h.update(bloco)
    return h.hexdigest()


def _sha_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


# ---------------------------------------------------------------------------
# As regras de validacao, isoladas
# ---------------------------------------------------------------------------
def teste_validacao_de_download():
    with PastaTemp() as tmp:
        parcial = os.path.join(tmp, "x.bin" + PARCIAL)
        escrever(parcial, b"a" * 500)

        r = validar_download(parcial, 500, 1000, 111.0, 111.0, True)
        igual(r.offset, 500, "parcial integro retoma de onde parou")

        r = validar_download(parcial, 400, 1000, 111.0, 111.0, True)
        igual(r.offset, 0, "tamanho do parcial diferente do registrado: recomeca")
        checar("400" in r.motivo or "500" in r.motivo,
               "e diz os dois tamanhos no motivo")

        r = validar_download(parcial, 500, 1000, 999.0, 111.0, True)
        igual(r.offset, 0, "arquivo mudou no servidor: recomeca")
        checar("mudou no servidor" in r.motivo, "com o motivo certo")

        r = validar_download(parcial, 500, 1000, 111.0, 111.0, False)
        igual(r.offset, 0, "servidor sem REST: recomeca")

        r = validar_download(os.path.join(tmp, "sumiu" + PARCIAL), 500, 1000,
                             111.0, 111.0, True)
        igual(r.offset, 0, "parcial que sumiu do disco: recomeca")

        r = validar_download(parcial, 500, 300, 111.0, 111.0, True)
        igual(r.offset, 0, "parcial maior que o arquivo remoto: recomeca")

        r = validar_download(parcial, 500, 1000, 112.5, 111.0, True)
        igual(r.offset, 500,
              "diferenca de data dentro da tolerancia de 2s ainda retoma")


def teste_validacao_de_upload():
    igual(validar_upload(500, 500, True).offset, 500,
          "destino com exatamente o que foi enviado: retoma")
    igual(validar_upload(700, 500, True).offset, 0,
          "destino MAIOR que o registrado: reenvia do zero (alguem mexeu la)")
    igual(validar_upload(300, 500, True).offset, 0,
          "destino menor que o registrado: reenvia do zero")
    igual(validar_upload(-1, 500, True).offset, 0,
          "servidor que nao informa tamanho nao ganha o beneficio da duvida")
    igual(validar_upload(500, 500, False).offset, 0,
          "servidor sem retomada de envio: do zero")
    igual(validar_upload(0, 0, True).offset, 0, "sem progresso anterior")


def teste_classificacao_de_erro():
    from ftpzilla.remotes.base import (ErroAutenticacao, ErroCertificado,
                                       ErroPermanente, ErroTransitorio)
    from ftpzilla import transfer
    igual(classificar(ErroTransitorio("timeout")), transfer.TRANSITORIO,
          "timeout vale repetir")
    igual(classificar(ErroAutenticacao("530")), transfer.AUTENTICACAO,
          "senha recusada NAO se repete sozinha")
    igual(classificar(ErroPermanente("550")), transfer.PERMANENTE,
          "arquivo inexistente nao melhora repetindo")
    igual(classificar(ErroCertificado("h", "f")), transfer.CONFIANCA,
          "certificado desconhecido precisa do usuario")
    igual(classificar(ConnectionResetError()), transfer.TRANSITORIO,
          "conexao derrubada vale repetir")
    cheio = OSError("disco cheio")
    cheio.errno = 28
    igual(classificar(cheio), transfer.PERMANENTE,
          "disco cheio nao melhora repetindo")


# ---------------------------------------------------------------------------
# Contra o servidor
# ---------------------------------------------------------------------------
def teste_download_cortado_retoma_do_ponto_certo():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo()
        escrever(os.path.join(raiz, "grande.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)
        alvo = os.path.join(destino, "grande.bin")

        with servidores.servidor_ftp(raiz, cortar_em=CORTE, cortes=1) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            r = FtpRemote(site)
            r.conectar()
            try:
                checar(r.resume_download, "o servidor aceita REST")
                parcial = alvo + PARCIAL
                caiu = False
                with open(parcial, "wb") as f:
                    try:
                        r.baixar("/grande.bin", f)
                    except Exception:
                        caiu = True
                checar(caiu, "a primeira tentativa caiu no meio")
                feitos = os.path.getsize(parcial)
                checar(0 < feitos < TAM,
                       "sobrou um parcial de %d bytes" % feitos)
            finally:
                r.fechar()

            # segunda tentativa, em conexao nova, retomando do offset
            r = FtpRemote(site)
            r.conectar()
            try:
                with open(parcial, "r+b") as f:
                    f.seek(feitos)
                    r.baixar("/grande.bin", f, offset=feitos)
                os.replace(parcial, alvo)
            finally:
                r.fechar()

            igual(os.path.getsize(alvo), TAM, "o arquivo tem o tamanho certo")
            igual(_sha(alvo), _sha_bytes(dados),
                  "e o HASH bate: os dois pedacos se encaixaram na ordem certa")


def teste_fila_retoma_sozinha():
    """O cenario real: a conexao cai, a fila espera, tenta de novo e termina."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo()
        escrever(os.path.join(raiz, "grande.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)

        with servidores.servidor_ftp(raiz, cortar_em=CORTE, cortes=2) as (h, p):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=h, porta=p,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = GerenciadorFila(sites,
                                store=FilaStore(os.path.join(tmp, "fila.db")),
                                max_global=1).abrir()
            try:
                d = destino.replace("\\", "/")
                itens = g.enfileirar(g.montar_itens(site, BAIXAR, [
                    ("/grande.bin", d + "/grande.bin", TAM, 0)]))
                checar(g.esperar_vazia(120), "a fila terminou")
                item = itens[0]
                igual(item.estado, CONCLUIDO,
                      "concluiu apesar de a conexao ter caido duas vezes")
                alvo = os.path.join(destino, "grande.bin")
                igual(os.path.getsize(alvo), TAM, "tamanho certo")
                igual(_sha(alvo), _sha_bytes(dados),
                      "hash certo depois de retomar sozinha")
                checar(not os.path.exists(alvo + PARCIAL),
                       "o parcial foi promovido a arquivo final")
            finally:
                g.fechar()


def teste_progresso_nao_consome_tentativas():
    """Um arquivo grande num link ruim cai varias vezes. Se cada queda
    consumisse uma tentativa, ele nunca terminaria - mesmo andando um pouco
    a cada vez. Andar tem que zerar o contador."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo(600 * 1024)
        escrever(os.path.join(raiz, "g.bin"), dados)
        destino = os.path.join(tmp, "baixado").replace("\\", "/")
        os.makedirs(destino, exist_ok=True)

        # corta SEMPRE, a cada 80 KB: sao quase 8 quedas, mais que o limite
        # de tentativas - so termina porque o progresso zera o contador
        with servidores.servidor_ftp(raiz, cortar_em=80 * 1024) as (h, p):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=h, porta=p,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = GerenciadorFila(sites,
                                store=FilaStore(os.path.join(tmp, "fila.db")),
                                max_global=1).abrir()
            try:
                itens = g.enfileirar(g.montar_itens(site, BAIXAR, [
                    ("/g.bin", destino + "/g.bin", len(dados), 0)]))
                checar(g.esperar_vazia(180), "a fila terminou")
                igual(itens[0].estado, CONCLUIDO,
                      "concluiu com mais quedas que o limite de tentativas")
                checar(itens[0].tentativas <= MAX_TENTATIVAS,
                       "o contador nunca estourou (ficou em %d)"
                       % itens[0].tentativas)
                alvo = os.path.join(tmp, "baixado", "g.bin")
                igual(_sha(alvo), _sha_bytes(dados),
                      "e o arquivo montado de %d pedacos tem o hash certo"
                      % (len(dados) // (80 * 1024) + 1))
            finally:
                g.fechar()


def teste_parcial_divergente_recomeca():
    """O caso perigoso: o arquivo mudou no servidor enquanto a transferencia
    estava parada. Retomar montaria um Frankenstein com o tamanho certo."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        original = _conteudo(300 * 1024)
        escrever(os.path.join(raiz, "mutante.bin"), original)
        destino = os.path.join(tmp, "baixado")
        os.makedirs(destino, exist_ok=True)
        alvo = os.path.join(destino, "mutante.bin")

        # parcial "de uma sessao anterior", com conteudo que nao e do arquivo
        escrever(alvo + PARCIAL, b"\x00" * 100 * 1024)

        with servidores.servidor_ftp(raiz) as (h, p):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=h, porta=p,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = GerenciadorFila(sites,
                                store=FilaStore(os.path.join(tmp, "fila.db")),
                                max_global=1).abrir()
            try:
                item = g.montar_itens(site, BAIXAR, [
                    ("/mutante.bin", destino.replace("\\", "/")
                     + "/mutante.bin", len(original), 0)])[0]
                item.bytes_feitos = 100 * 1024
                item.mtime = 1.0          # data que nao bate com a do servidor
                g.enfileirar([item])
                checar(g.esperar_vazia(60), "a fila terminou")
                igual(item.estado, CONCLUIDO, "concluiu")
                igual(_sha(alvo), _sha_bytes(original),
                      "recomecou do zero e entregou o arquivo certo, em vez "
                      "de colar o parcial velho no arquivo novo")
            finally:
                g.fechar()


def teste_envio_retomado():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        dados = _conteudo(200 * 1024)
        local = os.path.join(tmp, "local", "envio.bin")
        escrever(local, dados)

        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            r = FtpRemote(site)
            r.conectar()
            try:
                checar(r.resume_upload, "o servidor aceita retomada de envio")
                metade = len(dados) // 2
                r.enviar(io.BytesIO(dados[:metade]), "/envio.bin",
                         tamanho=metade)
                igual(r.stat("/envio.bin").size, metade,
                      "a primeira metade subiu")

                r.enviar(io.BytesIO(dados[metade:]), "/envio.bin",
                         tamanho=len(dados) - metade, offset=metade)
                chegou = os.path.join(raiz, "envio.bin")
                igual(os.path.getsize(chegou), len(dados),
                      "o arquivo ficou completo")
                igual(_sha(chegou), _sha_bytes(dados),
                      "e o hash bate: REST no envio colou no lugar certo")
            finally:
                r.fechar()


def teste_conexao_ociosa_e_testada():
    """Servidor FTP derruba sessao parada sem avisar. O pool tem que
    descobrir isso ANTES de entregar a conexao para uma transferencia."""
    _precisa()
    from ftpzilla.pool import PoolConexoes
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        escrever(os.path.join(raiz, "a.txt"), b"x")
        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            pool = PoolConexoes(site, maximo=2)
            try:
                c1 = pool.pegar()
                checar(c1.conectado, "o pool entregou uma conexao conectada")
                pool.devolver(c1)
                c2 = pool.pegar()
                checar(c2 is c1, "a conexao foi reaproveitada")
                pool.devolver(c2, suja=True)
                c3 = pool.pegar()
                checar(c3 is not c1,
                       "conexao marcada como suja nao volta para o pool")
                pool.devolver(c3)
                igual(pool.reduzir_teto("teste"), 1,
                       "o teto cai quando o servidor reclama de excesso")
            finally:
                pool.fechar()


def teste_verificacao_de_integridade():
    """Conferir tamanho nao detecta bytes trocados de lugar. Quando o
    servidor sabe calcular hash, o hash e comparado; quando nao sabe, o
    programa diz isso em vez de fingir que verificou."""
    _precisa()
    from ftpzilla.transfer import conferir_integridade

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        dados = _conteudo(50 * 1024)
        escrever(os.path.join(raiz, "a.bin"), dados)
        copia = os.path.join(tmp, "copia.bin")
        escrever(copia, dados)

        with servidores.servidor_ftp(raiz) as (h, p):
            r = FtpRemote(Site(nome="t", kind="ftp", host=h, porta=p,
                               usuario=servidores.USUARIO,
                               senha=servidores.SENHA, tls_modo="nenhum"))
            r.conectar()
            try:
                ok, detalhe = conferir_integridade(r, "/a.bin", copia,
                                                   len(dados))
                checar(ok, "copia identica passa na verificacao")
                checar("conferido" in detalhe,
                       "e a descricao diz o que foi conferido (%s)" % detalhe)

                trocado = os.path.join(tmp, "trocado.bin")
                # mesmo tamanho, bytes fora de ordem: exatamente o defeito
                # que uma retomada errada produz
                escrever(trocado, dados[25 * 1024:] + dados[:25 * 1024])
                ok, detalhe = conferir_integridade(r, "/a.bin", trocado,
                                                   len(dados))
                if r.hash_remoto("/a.bin"):
                    checar(not ok, "arquivo com bytes fora de ordem e recusado")
                else:
                    checar(ok and "nao calcula hash" in detalhe,
                           "sem hash no servidor, o programa avisa que so "
                           "conferiu o tamanho")

                curto = os.path.join(tmp, "curto.bin")
                escrever(curto, dados[:100])
                ok, detalhe = conferir_integridade(r, "/a.bin", curto,
                                                   len(dados))
                checar(not ok, "tamanho diferente reprova sempre")
            finally:
                r.fechar()

    # o pyftpdlib nao implementa XMD5/XCRC, entao a comparacao por hash e
    # exercitada com um servidor de mentira que sabe responder
    import hashlib as _h

    class ServidorComHash:
        def __init__(self, dados):
            self.valor = _h.md5(dados).hexdigest()

        def hash_remoto(self, caminho, algoritmo="md5"):
            return ("md5", self.valor)

    with PastaTemp() as tmp:
        dados = _conteudo(20 * 1024)
        bom = os.path.join(tmp, "bom.bin")
        escrever(bom, dados)
        trocado = os.path.join(tmp, "trocado.bin")
        escrever(trocado, dados[10 * 1024:] + dados[:10 * 1024])

        srv = ServidorComHash(dados)
        ok, detalhe = conferir_integridade(srv, "/x", bom, len(dados))
        checar(ok, "com hash do servidor, o arquivo correto passa")
        ok, detalhe = conferir_integridade(srv, "/x", trocado, len(dados))
        checar(not ok,
               "e o arquivo com bytes fora de ordem (mesmo tamanho!) e "
               "recusado - que e o defeito de uma retomada errada")
        checar("MD5" in detalhe, "com a mensagem dizendo qual hash falhou")


TESTES = [teste_validacao_de_download, teste_validacao_de_upload,
          teste_classificacao_de_erro,
          teste_download_cortado_retoma_do_ponto_certo,
          teste_fila_retoma_sozinha, teste_progresso_nao_consome_tentativas,
          teste_parcial_divergente_recomeca, teste_envio_retomado,
          teste_conexao_ociosa_e_testada, teste_verificacao_de_integridade]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Retomada e repeticao"))
