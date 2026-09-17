"""Disco local visto como Remote: navegacao, escrita atomica, mtime."""
from __future__ import annotations

import io
import os
import sys
import threading
import time

import ajuda
from ajuda import PastaTemp, checar, escrever, igual, levanta

from ftpzilla.remotes.base import Cancelado, ErroPermanente
from ftpzilla.remotes.local import PARCIAL, LocalRemote

WINDOWS = sys.platform == "win32"


def _remoto(raiz: str) -> tuple:
    r = LocalRemote()
    return r, r.normalizar(raiz)


def teste_caminhos_do_windows():
    r = LocalRemote()
    if not WINDOWS:
        ajuda.pular("traducao de caminho so vale no Windows")
    igual(r.normalizar("c:\\DEV\\x"), "C:/DEV/x", "letra da unidade em maiuscula")
    igual(r.normalizar("C:/DEV/../DEV/x"), "C:/DEV/x", "'..' resolvido")
    igual(r.nativo("C:/DEV/x"), "C:\\DEV\\x", "volta para o formato do Windows")
    igual(r.pai("C:/"), "/", "acima da raiz da unidade fica a lista de unidades")
    igual(r.juntar("/", "C:"), "C:/", "entrar numa unidade a partir da raiz")
    checar(any(e.name.endswith(":") for e in r.listar("/")),
           "a raiz virtual lista as unidades")


def teste_listar_e_stat():
    with PastaTemp() as tmp:
        escrever(os.path.join(tmp, "a.txt"), b"12345")
        os.makedirs(os.path.join(tmp, "sub"))
        r, raiz = _remoto(tmp)

        itens = {e.name: e for e in r.listar(raiz)}
        igual(sorted(itens), ["a.txt", "sub"], "lista arquivos e pastas")
        igual(itens["a.txt"].size, 5, "tamanho do arquivo")
        checar(itens["sub"].is_dir, "a pasta e marcada como pasta")
        checar(itens["sub"].size == 0, "pasta nao reporta tamanho")
        checar(itens["a.txt"].mtime > 0, "mtime preenchido")

        e = r.stat(r.juntar(raiz, "a.txt"))
        igual(e.size, 5, "stat traz o tamanho")
        checar(r.stat(r.juntar(raiz, "nao_existe")) is None,
               "stat de inexistente devolve None, nao excecao")
        checar(r.existe(raiz), "existe() na propria pasta")


def teste_erro_de_pasta_inexistente():
    r = LocalRemote()
    levanta(ErroPermanente, lambda: r.listar(r.normalizar("Z:/nao/existe/mesmo")),
            "listar pasta inexistente vira ErroPermanente")


def teste_criar_renomear_apagar():
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        nova = r.juntar(raiz, "nova")
        r.criar_pasta(nova)
        checar(r.existe(nova), "criar_pasta")

        fundo = r.juntar(nova, "a/b/c")
        r.criar_pastas(fundo)
        checar(r.existe(fundo), "criar_pastas cria a arvore inteira")
        r.criar_pastas(fundo)
        ok = True
        try:
            r.criar_pastas(fundo)
        except Exception:
            ok = False
        checar(ok, "criar_pastas em pasta existente nao reclama")

        arq = r.juntar(nova, "x.txt")
        escrever(r.nativo(arq), b"oi")
        r.renomear(arq, r.juntar(nova, "y.txt"))
        checar(r.existe(r.juntar(nova, "y.txt")) and not r.existe(arq),
               "renomear")

        r.apagar_arquivo(r.juntar(nova, "y.txt"))
        checar(not r.existe(r.juntar(nova, "y.txt")), "apagar_arquivo")

        r.apagar_arvore(nova)
        checar(not r.existe(nova), "apagar_arvore leva o conteudo junto")


def teste_mtime():
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        arq = r.juntar(raiz, "d.txt")
        escrever(r.nativo(arq), b"x")
        quando = time.time() - 86400
        r.definir_mtime(arq, quando)
        obtido = r.stat(arq).mtime
        checar(abs(obtido - quando) < 2.0,
               "definir_mtime grava a data (tolerancia de 2s)")


def teste_baixar_com_offset_e_limite():
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        arq = r.juntar(raiz, "dados.bin")
        escrever(r.nativo(arq), bytes(range(256)) * 4)

        buf = io.BytesIO()
        n = r.baixar(arq, buf)
        igual(n, 1024, "baixa o arquivo inteiro")
        igual(buf.getvalue()[:4], bytes([0, 1, 2, 3]), "conteudo confere")

        buf = io.BytesIO()
        r.baixar(arq, buf, offset=1000)
        igual(len(buf.getvalue()), 24, "offset pula o comeco")

        buf = io.BytesIO()
        r.baixar(arq, buf, offset=256, limite=10)
        igual(buf.getvalue(), bytes(range(10)),
              "limite le so a faixa pedida (usado na segmentacao)")


def teste_callback_recebe_delta():
    """Contrato do cb: delta, nunca acumulado. Se algum backend mandar o
    total, a barra de progresso passa de 100% e o ETA vira lixo."""
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        arq = r.juntar(raiz, "g.bin")
        escrever(r.nativo(arq), b"x" * (700 * 1024))   # forca varios blocos

        pedacos = []
        r.baixar(arq, io.BytesIO(), cb=pedacos.append)
        checar(len(pedacos) > 1, "o callback foi chamado mais de uma vez")
        igual(sum(pedacos), 700 * 1024, "a soma dos deltas e o tamanho total")
        checar(all(p > 0 for p in pedacos), "nenhum delta vazio")


def teste_escrita_atomica():
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        alvo = r.juntar(raiz, "saida.bin")
        r.enviar(io.BytesIO(b"a" * 100), alvo, tamanho=100)
        igual(r.stat(alvo).size, 100, "enviar grava o arquivo")
        checar(not os.path.exists(r.nativo(alvo) + PARCIAL),
               "o parcial some quando termina")


def teste_cancelamento_preserva_parcial():
    """Cancelar tem que deixar o .part no disco - e dele que o resume parte."""
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        alvo = r.juntar(raiz, "cancelado.bin")
        cancelar = threading.Event()

        def no_primeiro_bloco(_n):
            cancelar.set()

        levanta(Cancelado,
                lambda: r.enviar(io.BytesIO(b"z" * (900 * 1024)), alvo,
                                 cb=no_primeiro_bloco, cancelar=cancelar),
                "cancelar levanta Cancelado")
        checar(not os.path.exists(r.nativo(alvo)),
               "o arquivo final nao chega a existir")
        checar(os.path.exists(r.nativo(alvo) + PARCIAL),
               "o parcial fica para o resume usar")


def teste_enviar_com_offset():
    with PastaTemp() as tmp:
        r, raiz = _remoto(tmp)
        alvo = r.juntar(raiz, "retomado.bin")
        parcial = r.nativo(alvo) + PARCIAL
        escrever(parcial, b"A" * 500)

        r.enviar(io.BytesIO(b"B" * 500), alvo, offset=500)
        with open(r.nativo(alvo), "rb") as f:
            dados = f.read()
        igual(len(dados), 1000, "o arquivo final tem as duas partes")
        checar(dados[:500] == b"A" * 500 and dados[500:] == b"B" * 500,
               "a parte antiga foi preservada e a nova, anexada")


def teste_capacidades():
    r = LocalRemote()
    checar(r.is_local, "o disco se declara local")
    checar(r.resume_download and r.resume_upload, "disco local sempre retoma")
    checar(not r.segmentavel,
           "disco local nao e segmentado: ler o mesmo disco em paralelo atrapalha")
    igual(r.max_conexoes, 1, "uma 'conexao' basta para o disco")
    igual(r.case_sensitive, not WINDOWS,
          "sensibilidade a maiuscula segue o sistema")


TESTES = [teste_caminhos_do_windows, teste_listar_e_stat,
          teste_erro_de_pasta_inexistente, teste_criar_renomear_apagar,
          teste_mtime, teste_baixar_com_offset_e_limite,
          teste_callback_recebe_delta, teste_escrita_atomica,
          teste_cancelamento_preserva_parcial, teste_enviar_com_offset,
          teste_capacidades]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Disco local"))
