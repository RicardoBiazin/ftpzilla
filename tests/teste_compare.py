"""Comparacao de pastas e plano de sincronizacao."""
from __future__ import annotations

import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla import compare
from ftpzilla.compare import (BIDIRECIONAL, DIR_MAIS_NOVA, ESPELHAR_ENVIO,
                              ESQ_MAIS_NOVA, IGUAL, SO_DIREITA, SO_ESQUERDA,
                              TAMANHO_DIFERE, TIPO_DIFERE, Comparador,
                              ENVIAR_NOVOS, mesmo_arquivo, resumir_plano)
from ftpzilla.remotes.base import Entry
from ftpzilla.remotes.local import LocalRemote


def _e(nome, tam=10, mtime=1000.0, is_dir=False, etag=""):
    return Entry(name=nome, size=tam, mtime=mtime, is_dir=is_dir, etag=etag)


# ---------------------------------------------------------------------------
def teste_mesmo_arquivo():
    igual(mesmo_arquivo(_e("a"), _e("a"), True), True, "iguais")
    igual(mesmo_arquivo(_e("a", tam=10), _e("a", tam=11), True), False,
          "tamanho diferente nunca e o mesmo arquivo")
    igual(mesmo_arquivo(_e("a", mtime=1000), _e("a", mtime=1001.5), True), True,
          "diferenca de 1,5s cabe na tolerancia (FAT arredonda para 2s)")
    igual(mesmo_arquivo(_e("a", mtime=1000), _e("a", mtime=1010), True), False,
          "10s de diferenca ja e outro arquivo")
    igual(mesmo_arquivo(_e("a", mtime=1000), _e("a", mtime=9999), False), True,
          "sem usar data, tamanho igual basta")
    igual(mesmo_arquivo(_e("a", mtime=0), _e("a", mtime=1000), True), True,
          "data desconhecida de um lado nao serve para NEGAR igualdade")
    igual(mesmo_arquivo(_e("a", tam=1, etag="x"), _e("a", tam=1, etag="x"),
                        True), True, "etag igual encerra a discussao")
    igual(mesmo_arquivo(_e("a"), _e("a", is_dir=True), True), False,
          "arquivo nunca e igual a pasta")


def teste_regra_do_mtime():
    """A regra que impede a sincronizacao de virar um moinho: se um dos lados
    nao consegue gravar a data, comparar por data faria TODO arquivo parecer
    diferente para sempre."""
    class Falso(LocalRemote):
        def __init__(self, preserva):
            super().__init__(None)
            self.preserva_mtime = preserva

    c = Comparador(Falso(True), Falso(True))
    checar(c.usar_mtime, "dois lados que preservam a data: compara por data")

    c = Comparador(Falso(True), Falso(False))
    checar(not c.usar_mtime,
           "um lado que NAO preserva (FTP sem MFMT): cai para tamanho, em vez "
           "de reenviar a pasta inteira toda vez")

    c = Comparador(Falso(True), Falso(False), modo=compare.DATA)
    checar(c.usar_mtime, "o usuario pode forcar a comparacao por data")


def teste_estados():
    with PastaTemp() as tmp:
        esq = os.path.join(tmp, "esq")
        dir_ = os.path.join(tmp, "dir")
        for d in (esq, dir_):
            os.makedirs(d, exist_ok=True)

        escrever(os.path.join(esq, "igual.txt"), b"1234567890")
        escrever(os.path.join(dir_, "igual.txt"), b"1234567890")
        quando = time.time() - 500
        os.utime(os.path.join(esq, "igual.txt"), (quando, quando))
        os.utime(os.path.join(dir_, "igual.txt"), (quando, quando))

        escrever(os.path.join(esq, "so_esq.txt"), b"x")
        escrever(os.path.join(dir_, "so_dir.txt"), b"y")

        escrever(os.path.join(esq, "tamanho.txt"), b"a" * 10)
        escrever(os.path.join(dir_, "tamanho.txt"), b"a" * 20)
        os.utime(os.path.join(esq, "tamanho.txt"), (quando, quando))
        os.utime(os.path.join(dir_, "tamanho.txt"), (quando, quando))

        escrever(os.path.join(esq, "nova.txt"), b"zz")
        escrever(os.path.join(dir_, "nova.txt"), b"zz")
        os.utime(os.path.join(dir_, "nova.txt"), (quando, quando))

        os.makedirs(os.path.join(esq, "pasta"), exist_ok=True)
        escrever(os.path.join(dir_, "pasta"), b"nao sou pasta")

        c = Comparador(LocalRemote(), LocalRemote())
        pares = {p.rel: p for p in c.comparar_pasta(
            esq.replace("\\", "/"), dir_.replace("\\", "/"))}

        igual(pares["igual.txt"].estado, IGUAL, "arquivo identico")
        igual(pares["so_esq.txt"].estado, SO_ESQUERDA, "so do lado esquerdo")
        igual(pares["so_dir.txt"].estado, SO_DIREITA, "so do lado direito")
        igual(pares["tamanho.txt"].estado, TAMANHO_DIFERE,
              "mesma data, tamanho diferente")
        igual(pares["nova.txt"].estado, ESQ_MAIS_NOVA,
              "mesma coisa, mas a da esquerda e mais nova")
        igual(pares["pasta"].estado, TIPO_DIFERE,
              "pasta de um lado e arquivo do outro")

        mapa = c.mapa_para_painel(list(pares.values()))
        igual(mapa["so_esq.txt"], SO_ESQUERDA,
              "o mapa para o painel usa o nome como chave")


def teste_arvore_e_filtros():
    with PastaTemp() as tmp:
        esq = os.path.join(tmp, "esq")
        dir_ = os.path.join(tmp, "dir")
        escrever(os.path.join(esq, "a", "b", "fundo.txt"), b"1")
        escrever(os.path.join(esq, "lixo.tmp"), b"2")
        escrever(os.path.join(esq, "bom.txt"), b"3")
        os.makedirs(dir_, exist_ok=True)

        c = Comparador(LocalRemote(), LocalRemote())
        pares = list(c.comparar_arvore(esq.replace("\\", "/"),
                                       dir_.replace("\\", "/")))
        rels = sorted(p.rel for p in pares)
        checar("a/b/fundo.txt" in rels, "a arvore inteira foi percorrida")
        checar("a" in rels and "a/b" in rels,
               "as pastas tambem aparecem (quem sincroniza precisa cria-las)")

        c = Comparador(LocalRemote(), LocalRemote(), excluir=["*.tmp"])
        rels = sorted(p.rel for p in c.comparar_arvore(
            esq.replace("\\", "/"), dir_.replace("\\", "/")))
        checar("lixo.tmp" not in rels, "o filtro de exclusao foi aplicado")
        checar("bom.txt" in rels, "e nao levou junto o que devia ficar")


def teste_plano_enviar_novos():
    pares = [
        compare.ParComparado("so_esq.txt", _e("so_esq.txt"), None, SO_ESQUERDA),
        compare.ParComparado("so_dir.txt", None, _e("so_dir.txt"), SO_DIREITA),
        compare.ParComparado("dif.txt", _e("dif.txt"), _e("dif.txt", tam=99),
                             TAMANHO_DIFERE),
        compare.ParComparado("igual.txt", _e("igual.txt"), _e("igual.txt"),
                             IGUAL),
        compare.ParComparado("pasta", _e("pasta", is_dir=True), None,
                             SO_ESQUERDA, is_dir=True),
    ]
    c = Comparador(LocalRemote(), LocalRemote())
    acoes = c.plano(pares, ENVIAR_NOVOS, "C:/esq", "C:/dir")
    tipos = [a["acao"] for a in acoes]
    checar("enviar" in tipos, "envia o que so existe do lado de ca")
    checar("criar_pasta_dir" in tipos, "e cria a pasta que falta la")
    checar("baixar" not in tipos,
           "'enviar novos' nao baixa nada - o sentido e um so")
    checar(not any(t.startswith("apagar") for t in tipos),
           "e nao apaga nada: so o modo espelho apaga")
    checar("igual.txt" not in [a["par"].rel for a in acoes],
           "arquivo identico fica de fora")
    igual(tipos.index("criar_pasta_dir"), 0,
          "a pasta e criada antes de os arquivos entrarem nela")


def teste_plano_espelho_apaga():
    pares = [
        compare.ParComparado("sobrando.txt", None, _e("sobrando.txt"),
                             SO_DIREITA),
        compare.ParComparado("p/a.txt", None, _e("a.txt"), SO_DIREITA),
        compare.ParComparado("p", None, _e("p", is_dir=True), SO_DIREITA,
                             is_dir=True),
    ]
    c = Comparador(LocalRemote(), LocalRemote())
    acoes = c.plano(pares, ESPELHAR_ENVIO, "C:/esq", "C:/dir")
    tipos = [a["acao"] for a in acoes]
    checar(all(t == "apagar_dir" for t in tipos),
           "espelho de envio apaga o que sobra no destino")
    ultimos = [a["par"].rel for a in acoes]
    checar(ultimos.index("p/a.txt") < ultimos.index("p"),
           "o arquivo e apagado antes da pasta que o contem")


def teste_plano_bidirecional():
    pares = [
        compare.ParComparado("esq_nova.txt", _e("esq_nova.txt", mtime=2000),
                             _e("esq_nova.txt", mtime=1000), ESQ_MAIS_NOVA),
        compare.ParComparado("dir_nova.txt", _e("dir_nova.txt", mtime=1000),
                             _e("dir_nova.txt", mtime=2000), DIR_MAIS_NOVA),
        compare.ParComparado("conflito.txt", _e("conflito.txt", tam=1),
                             _e("conflito.txt", tam=2), TAMANHO_DIFERE),
    ]
    c = Comparador(LocalRemote(), LocalRemote())
    acoes = {a["par"].rel: a["acao"] for a in c.plano(
        pares, BIDIRECIONAL, "C:/esq", "C:/dir")}
    igual(acoes.get("esq_nova.txt"), "enviar", "a mais nova vai para o outro lado")
    igual(acoes.get("dir_nova.txt"), "baixar", "e vice-versa")
    checar("conflito.txt" not in acoes,
           "diferente sem data confiavel nao e resolvido em silencio: "
           "sobrescrever perderia o trabalho de alguem")


def teste_resumo_do_plano():
    c = Comparador(LocalRemote(), LocalRemote())
    acoes = c.plano([compare.ParComparado("a", _e("a"), None, SO_ESQUERDA)],
                    ENVIAR_NOVOS, "C:/e", "C:/d")
    texto = resumir_plano(acoes)
    checar("enviar" in texto, "o resumo diz o que vai acontecer (%s)" % texto)
    igual(resumir_plano([]), "nada a fazer", "plano vazio e dito com clareza")


def teste_contra_servidor_ftp():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado")
    from ftpzilla.remotes.ftp import FtpRemote
    from ftpzilla.sites import Site

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        local = os.path.join(tmp, "local")
        escrever(os.path.join(raiz, "so_la.txt"), b"1")
        escrever(os.path.join(raiz, "ambos.txt"), b"12345")
        escrever(os.path.join(local, "so_aqui.txt"), b"2")
        escrever(os.path.join(local, "ambos.txt"), b"12345")

        with servidores.servidor_ftp(raiz) as (h, p):
            r = FtpRemote(Site(nome="t", kind="ftp", host=h, porta=p,
                               usuario=servidores.USUARIO,
                               senha=servidores.SENHA, tls_modo="nenhum"))
            r.conectar()
            try:
                c = Comparador(LocalRemote(), r)
                pares = {x.rel: x for x in c.comparar_pasta(
                    local.replace("\\", "/"), "/")}
                igual(pares["so_aqui.txt"].estado, SO_ESQUERDA,
                      "arquivo que so existe no disco")
                igual(pares["so_la.txt"].estado, SO_DIREITA,
                      "arquivo que so existe no servidor")
                checar(pares["ambos.txt"].estado in (IGUAL, TAMANHO_DIFERE,
                                                     ESQ_MAIS_NOVA,
                                                     DIR_MAIS_NOVA),
                       "o arquivo dos dois lados foi comparado (%s)"
                       % pares["ambos.txt"].estado)
                checar(c.usar_mtime,
                       "com MFMT no servidor, a comparacao usa a data")
            finally:
                r.fechar()

        with servidores.servidor_ftp(raiz, sem_mfmt=True) as (h, p):
            r = FtpRemote(Site(nome="t", kind="ftp", host=h, porta=p,
                               usuario=servidores.USUARIO,
                               senha=servidores.SENHA, tls_modo="nenhum"))
            r.conectar()
            try:
                c = Comparador(LocalRemote(), r)
                checar(not c.usar_mtime,
                       "servidor sem MFMT desliga a comparacao por data")
                pares = {x.rel: x for x in c.comparar_pasta(
                    local.replace("\\", "/"), "/")}
                igual(pares["ambos.txt"].estado, IGUAL,
                      "e o arquivo de mesmo tamanho e dado como igual, em vez "
                      "de ser reenviado para sempre")
            finally:
                r.fechar()


TESTES = [teste_mesmo_arquivo, teste_regra_do_mtime, teste_estados,
          teste_arvore_e_filtros, teste_plano_enviar_novos,
          teste_plano_espelho_apaga, teste_plano_bidirecional,
          teste_resumo_do_plano, teste_contra_servidor_ftp]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Comparacao e sincronizacao"))
