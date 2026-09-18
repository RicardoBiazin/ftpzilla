"""O que acontece quando o arquivo ja existe no destino."""
from __future__ import annotations

import hashlib
import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla import conflitos
from ftpzilla.conflitos import (PULAR, RENOMEAR, RETOMAR, SE_MAIS_NOVO,
                                SOBRESCREVER, aplicar, detectar, nome_livre)
from ftpzilla.fila import GerenciadorFila
from ftpzilla.fila_store import BAIXAR, CONCLUIDO, ENVIAR, FilaStore
from ftpzilla.sites import GerenteSites, Site


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado")


# ---------------------------------------------------------------------------
# Deteccao
# ---------------------------------------------------------------------------
def teste_detecta_no_disco():
    with PastaTemp() as tmp:
        destino = os.path.join(tmp, "destino")
        escrever(os.path.join(destino, "existe.txt"), b"a" * 100)
        d = destino.replace("\\", "/")
        agora = time.time()

        pares = [("/existe.txt", d + "/existe.txt", 500, agora),
                 ("/novo.txt", d + "/novo.txt", 10, agora)]
        achados = detectar(pares, BAIXAR)
        igual(len(achados), 1, "so o que ja existe vira conflito")
        c = achados[0]
        igual(c.nome, "existe.txt", "o nome certo")
        igual(c.tamanho_destino, 100, "leu o tamanho do que esta la")
        igual(c.tamanho_origem, 500, "e o do que esta chegando")
        checar(c.destino_incompleto,
               "destino menor que a origem: da para oferecer 'continuar'")


def teste_reconhece_arquivo_igual():
    """Baixar a mesma coisa de novo e o caso mais comum. Precisa ser dito
    com clareza, senao a pessoa sobrescreve 300 arquivos a toa."""
    with PastaTemp() as tmp:
        destino = os.path.join(tmp, "destino")
        alvo = os.path.join(destino, "igual.bin")
        escrever(alvo, b"x" * 1000)
        quando = time.time() - 3600
        os.utime(alvo, (quando, quando))
        d = destino.replace("\\", "/")

        c = detectar([("/igual.bin", d + "/igual.bin", 1000, quando)],
                     BAIXAR)[0]
        checar(c.iguais, "mesmo tamanho e mesma data: e o mesmo arquivo")
        checar(not c.destino_incompleto, "e nao ha o que continuar")

        c = detectar([("/igual.bin", d + "/igual.bin", 1000, quando + 1.0)],
                     BAIXAR)[0]
        checar(c.iguais,
               "1 segundo de diferenca ainda e o mesmo arquivo (FAT arredonda)")

        c = detectar([("/igual.bin", d + "/igual.bin", 1000, quando + 600)],
                     BAIXAR)[0]
        checar(not c.iguais and c.origem_mais_nova,
               "10 minutos depois ja e uma versao mais nova")


def teste_detecta_no_servidor():
    _precisa()
    from ftpzilla.remotes.ftp import FtpRemote
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        escrever(os.path.join(raiz, "pasta", "la.txt"), b"ja estou aqui")
        with servidores.servidor_ftp(raiz) as (h, p):
            r = FtpRemote(Site(nome="t", kind="ftp", host=h, porta=p,
                               usuario=servidores.USUARIO,
                               senha=servidores.SENHA, tls_modo="nenhum"))
            r.conectar()
            try:
                pares = [("C:/local/la.txt", "/pasta/la.txt", 99, time.time()),
                         ("C:/local/outro.txt", "/pasta/outro.txt", 5, 0)]
                achados = detectar(pares, ENVIAR, r)
                igual(len(achados), 1, "achou o que ja esta no servidor")
                igual(achados[0].tamanho_destino, 13,
                      "com o tamanho que o servidor informou")
            finally:
                r.fechar()


def teste_consulta_uma_vez_por_pasta():
    """Um stat por arquivo custaria uma viagem ao servidor por linha. Com
    300 arquivos isso e a diferenca entre meio segundo e minutos."""
    class RemotoContador:
        def __init__(self):
            self.chamadas = []

        def listar(self, pasta):
            self.chamadas.append(pasta)
            return []

    r = RemotoContador()
    pares = [("o%d" % i, "/mesma/pasta/a%d.txt" % i, 1, 0) for i in range(50)]
    pares += [("x", "/outra/b.txt", 1, 0)]
    detectar(pares, ENVIAR, r)
    igual(sorted(r.chamadas), ["/mesma/pasta", "/outra"],
          "uma listagem por pasta, e nao uma por arquivo")


# ---------------------------------------------------------------------------
# Decisoes
# ---------------------------------------------------------------------------
def _cenario():
    agora = time.time()
    pares = [("/a.txt", "C:/d/a.txt", 100, agora),
             ("/b.txt", "C:/d/b.txt", 100, agora - 7200),
             ("/c.txt", "C:/d/c.txt", 100, agora)]
    achados = [
        conflitos.Conflito(0, "/a.txt", "C:/d/a.txt", 100, agora, 50,
                           agora - 3600),          # origem mais nova
        conflitos.Conflito(1, "/b.txt", "C:/d/b.txt", 100, agora - 7200, 100,
                           agora),                 # destino mais novo
    ]
    return pares, achados


def teste_sobrescrever_e_pular():
    pares, achados = _cenario()
    saida, acoes, pulados = aplicar(pares, achados, {0: SOBRESCREVER,
                                                     1: SOBRESCREVER})
    igual(len(saida), 3, "sobrescrever mantem todos")
    igual(pulados, 0, "e nao pula nada")

    saida, acoes, pulados = aplicar(pares, achados, {0: PULAR, 1: PULAR})
    igual(len(saida), 1, "pular deixa so o que nao tinha conflito")
    igual(saida[0][0], "/c.txt", "e e justamente o arquivo novo")
    igual(pulados, 2, "contou os dois pulados")


def teste_so_se_mais_novo():
    pares, achados = _cenario()
    saida, acoes, pulados = aplicar(pares, achados, {0: SE_MAIS_NOVO,
                                                     1: SE_MAIS_NOVO})
    origens = [p[0] for p in saida]
    checar("/a.txt" in origens,
           "o arquivo cuja origem e mais nova e transferido")
    checar("/b.txt" not in origens,
           "o que esta mais novo NO DESTINO e preservado")
    igual(pulados, 1, "e isso conta como pulado")


def teste_renomear_mantem_os_dois():
    pares, achados = _cenario()
    saida, acoes, pulados = aplicar(pares, achados, {0: RENOMEAR,
                                                     1: RENOMEAR})
    destinos = [p[1] for p in saida]
    igual(len(saida), 3, "nada e descartado")
    checar("C:/d/a (2).txt" in destinos,
           "o que chega ganha um nome novo (%s)" % destinos)
    checar("C:/d/a.txt" not in destinos,
           "e o arquivo que ja estava la nao e tocado")


def teste_nome_livre_nao_colide():
    igual(nome_livre("C:/d/a.txt", {"a.txt"}), "C:/d/a (2).txt", "primeiro livre")
    igual(nome_livre("C:/d/a.txt", {"a.txt", "a (2).txt"}), "C:/d/a (3).txt",
          "pula os que ja estao ocupados")
    igual(nome_livre("C:/d/semponto", {"semponto"}), "C:/d/semponto (2)",
          "arquivo sem extensao")
    igual(nome_livre("C:/d/a.tar.gz", {"a.tar.gz"}), "C:/d/a.tar (2).gz",
          "extensao composta: mexe so na ultima parte")


def teste_retomar_devolve_a_acao():
    pares, achados = _cenario()
    saida, acoes, _ = aplicar(pares, achados, {0: RETOMAR, 1: SOBRESCREVER})
    igual(acoes[0], RETOMAR,
          "a acao acompanha o item ate a transferencia")
    igual(acoes[2], SOBRESCREVER, "quem nao tinha conflito segue normal")


# ---------------------------------------------------------------------------
# De ponta a ponta
# ---------------------------------------------------------------------------
def teste_retomar_arquivo_ja_no_disco():
    """Metade do arquivo ja esta na pasta; 'continuar' tem que baixar so o
    que falta e entregar o arquivo integro."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        import random
        r = random.Random(7)
        dados = bytes(r.getrandbits(8) for _ in range(200 * 1024))
        escrever(os.path.join(raiz, "g.bin"), dados)
        destino = os.path.join(tmp, "baixado")
        alvo = os.path.join(destino, "g.bin")
        escrever(alvo, dados[:80 * 1024])       # "baixado pela metade antes"

        with servidores.servidor_ftp(raiz) as (h, p):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="t", kind="ftp", host=h, porta=p,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = GerenciadorFila(sites,
                                store=FilaStore(os.path.join(tmp, "f.db")),
                                max_global=1).abrir()
            try:
                itens = g.montar_itens(site, BAIXAR, [
                    ("/g.bin", destino.replace("\\", "/") + "/g.bin",
                     len(dados), os.path.getmtime(
                         os.path.join(raiz, "g.bin")))])
                itens[0].acao_existente = RETOMAR
                g.enfileirar(itens)
                checar(g.esperar_vazia(60), "a fila terminou")
                igual(itens[0].estado, CONCLUIDO, "concluiu")
                with open(alvo, "rb") as f:
                    baixado = f.read()
                igual(len(baixado), len(dados), "o arquivo ficou completo")
                igual(hashlib.sha256(baixado).hexdigest(),
                      hashlib.sha256(dados).hexdigest(),
                      "e integro: o pedaco que ja estava no disco foi "
                      "aproveitado e o resto caiu exatamente depois dele")
            finally:
                g.fechar()


def teste_sobrescrever_substitui_de_verdade():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        escrever(os.path.join(raiz, "a.txt"), b"conteudo NOVO do servidor")
        destino = os.path.join(tmp, "baixado")
        alvo = os.path.join(destino, "a.txt")
        escrever(alvo, b"conteudo velho que estava aqui")

        with servidores.servidor_ftp(raiz) as (h, p):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="t", kind="ftp", host=h, porta=p,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = GerenciadorFila(sites,
                                store=FilaStore(os.path.join(tmp, "f.db")),
                                max_global=1).abrir()
            try:
                itens = g.montar_itens(site, BAIXAR, [
                    ("/a.txt", destino.replace("\\", "/") + "/a.txt", 25, 0)])
                itens[0].acao_existente = SOBRESCREVER
                g.enfileirar(itens)
                checar(g.esperar_vazia(60), "a fila terminou")
                with open(alvo, "rb") as f:
                    igual(f.read(), b"conteudo NOVO do servidor",
                          "substituir troca o arquivo inteiro")
            finally:
                g.fechar()


TESTES = [teste_detecta_no_disco, teste_reconhece_arquivo_igual,
          teste_detecta_no_servidor, teste_consulta_uma_vez_por_pasta,
          teste_sobrescrever_e_pular, teste_so_se_mais_novo,
          teste_renomear_mantem_os_dois, teste_nome_livre_nao_colide,
          teste_retomar_devolve_a_acao, teste_retomar_arquivo_ja_no_disco,
          teste_sobrescrever_substitui_de_verdade]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Arquivo ja existe no destino"))
