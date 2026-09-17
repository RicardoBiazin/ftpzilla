"""Executa UM item da fila: o trabalho de mover os bytes.

Aqui moram as tres decisoes que separam um cliente que so funciona de um
cliente em que se pode confiar:

1. DA PARA RETOMAR? So depois de conferir que o parcial local e o arquivo
   remoto continuam sendo os mesmos de antes. Retomar sem conferir e como
   um cliente de FTP corrompe arquivo silenciosamente: o arquivo fica com o
   tamanho certo e o conteudo misturado, e ninguem descobre ate precisar
   dele.
2. VALE REPETIR? Timeout e conexao derrubada, sim. Senha recusada, nao -
   repetir senha errada tranca conta. Arquivo inexistente, nunca.
3. QUANTO ESPERAR? Espera exponencial com jitter. O jitter existe porque
   oito transferencias que caem juntas (o servidor reiniciou) voltariam
   juntas e derrubariam o servidor de novo.
"""
from __future__ import annotations

import os
import queue
import random
import threading
import time
from typing import Optional

from . import log
from .fila_store import BAIXAR, ENVIAR
from .remotes.base import (BLOCO, Cancelado, ErroAutenticacao, ErroCertificado,
                           ErroChaveDesconhecida, ErroPermanente, ErroRemoto,
                           ErroTransitorio, Remote)
from .remotes.local import PARCIAL, LocalRemote

logger = log.get()

#: quantas vezes um item transitorio e repetido antes de virar falha
MAX_TENTATIVAS = 5
#: teto da espera entre tentativas
ESPERA_MAXIMA = 60.0

# classificacao de erro
TRANSITORIO = "transitorio"
AUTENTICACAO = "auth"
PERMANENTE = "permanente"
CONFIANCA = "confianca"     # certificado ou chave de host: precisa do usuario


def classificar(exc: BaseException) -> str:
    """Erro -> o que fazer com ele."""
    if isinstance(exc, (ErroCertificado, ErroChaveDesconhecida)):
        return CONFIANCA
    if isinstance(exc, ErroAutenticacao):
        return AUTENTICACAO
    if isinstance(exc, ErroTransitorio):
        return TRANSITORIO
    if isinstance(exc, ErroPermanente):
        return PERMANENTE
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TRANSITORIO
    if isinstance(exc, OSError):
        # disco cheio / sem permissao no destino local nao melhora repetindo
        if getattr(exc, "errno", None) in (28, 13, 1):
            return PERMANENTE
        return TRANSITORIO
    if isinstance(exc, ErroRemoto):
        return TRANSITORIO
    return PERMANENTE


def espera(tentativa: int) -> float:
    """Espera exponencial com jitter, em segundos."""
    base = min(ESPERA_MAXIMA, 2.0 ** max(0, tentativa))
    return base * (0.5 + random.random())


# ---------------------------------------------------------------------------
# Retomada
# ---------------------------------------------------------------------------
class Retomada:
    """Resposta de 'da para continuar de onde parou?'."""

    def __init__(self, offset: int = 0, motivo: str = ""):
        self.offset = offset
        self.motivo = motivo

    def __bool__(self) -> bool:
        return self.offset > 0


def validar_download(caminho_parcial: str, bytes_feitos: int,
                     tamanho_remoto: int, mtime_remoto: float,
                     mtime_gravado: float, pode_retomar: bool) -> Retomada:
    """Decide se o .part pode continuar de onde parou.

    Tudo precisa bater: o parcial existe, tem exatamente o tamanho que a fila
    registrou, e o arquivo remoto continua com o mesmo tamanho e a mesma
    data. Qualquer divergencia recomeca do zero e diz por que - perder alguns
    minutos de download e muito melhor que entregar um arquivo corrompido.
    """
    if not bytes_feitos:
        return Retomada(0, "")
    if not pode_retomar:
        return Retomada(0, "o servidor nao aceita retomada")
    if not os.path.exists(caminho_parcial):
        return Retomada(0, "o arquivo parcial nao esta mais no disco")
    real = os.path.getsize(caminho_parcial)
    if real != bytes_feitos:
        return Retomada(0, "o parcial tem %d bytes, mas a fila registrou %d"
                        % (real, bytes_feitos))
    if tamanho_remoto >= 0 and bytes_feitos > tamanho_remoto:
        return Retomada(0, "o parcial e maior que o arquivo no servidor")
    if mtime_gravado and mtime_remoto and abs(mtime_remoto - mtime_gravado) > 2.0:
        return Retomada(0, "o arquivo mudou no servidor desde a interrupcao")
    return Retomada(bytes_feitos, "")


def validar_upload(tamanho_destino: int, bytes_feitos: int,
                   pode_retomar: bool) -> Retomada:
    """Decide se da para continuar um envio.

    Se o destino tem MAIS bytes do que a fila registrou, alguem (ou alguma
    outra sessao) mexeu no arquivo: reenviar do zero e a unica saida segura.
    Nao se confia no tamanho do servidor para "adivinhar" onde parar.
    """
    if not bytes_feitos or not pode_retomar:
        return Retomada(0, "" if not bytes_feitos else
                        "o servidor nao aceita retomada de envio")
    if tamanho_destino < 0:
        return Retomada(0, "o servidor nao informou o tamanho do destino")
    if tamanho_destino != bytes_feitos:
        return Retomada(0, "o destino tem %d bytes, mas a fila registrou %d"
                        % (tamanho_destino, bytes_feitos))
    return Retomada(bytes_feitos, "")


# ---------------------------------------------------------------------------
# Ponte servidor -> servidor
# ---------------------------------------------------------------------------
class _Cano:
    """Objeto de arquivo com write() de um lado e read() do outro.

    Serve para ligar dois Remotes sem passar pelo disco: uma thread baixa
    escrevendo aqui, a thread principal envia lendo daqui. O buffer e
    limitado de proposito - sem limite, baixar de um servidor rapido para um
    lento encheria a memoria com o arquivo inteiro.
    """

    def __init__(self, maximo: int = 16):
        self._fila: "queue.Queue" = queue.Queue(maxsize=maximo)
        self._resto = b""
        self._fim = False
        self._erro: Optional[BaseException] = None

    # lado que escreve
    def write(self, dados: bytes) -> int:
        self._fila.put(dados)
        return len(dados)

    def fechar_escrita(self, erro: BaseException = None) -> None:
        self._erro = erro
        self._fila.put(None)

    # lado que le
    def read(self, n: int = BLOCO) -> bytes:
        while len(self._resto) < n and not self._fim:
            pedaco = self._fila.get()
            if pedaco is None:
                self._fim = True
                break
            self._resto += pedaco
        if self._erro is not None:
            raise self._erro
        saida, self._resto = self._resto[:n], self._resto[n:]
        return saida


# ---------------------------------------------------------------------------
# A transferencia
# ---------------------------------------------------------------------------
class Resultado:
    def __init__(self, bytes_movidos: int = 0, offset_inicial: int = 0,
                 recomecou: bool = False, motivo: str = ""):
        self.bytes_movidos = bytes_movidos
        self.offset_inicial = offset_inicial
        self.recomecou = recomecou
        self.motivo = motivo

    @property
    def total(self) -> int:
        return self.offset_inicial + self.bytes_movidos


def executar(item, origem: Remote, destino: Remote, *, medidor=None,
             cancelar=None, store=None, limitador=None) -> Resultado:
    """Move um item. Levanta a excecao traduzida quando nao consegue."""
    if item.sentido == BAIXAR:
        return _baixar(item, origem, destino, medidor, cancelar, store,
                       limitador)
    if item.sentido == ENVIAR:
        return _enviar(item, origem, destino, medidor, cancelar, store,
                       limitador)
    return _entre_servidores(item, origem, destino, medidor, cancelar,
                             limitador)


def _callback(medidor, store, item, limitador):
    """Monta o callback de progresso uma vez so.

    Ele roda a cada bloco, dentro da thread de transferencia: nada de tocar
    em widget, nada de alocar. So soma, e de vez em quando avisa o banco.
    """
    def cb(n: int) -> None:
        if limitador is not None:
            limitador.consumir(n)
        if medidor is not None:
            medidor.bloco(n)
            if store is not None and item.id:
                store.progresso(item.id, item.offset_inicial + medidor.feitos)
    return cb


def _baixar(item, origem: Remote, destino: Remote, medidor, cancelar, store,
            limitador) -> Resultado:
    local = destino if isinstance(destino, LocalRemote) else LocalRemote()
    alvo = local.nativo(item.destino)
    parcial = alvo + PARCIAL

    info = origem.stat(item.origem)
    if info is None:
        raise ErroPermanente("O arquivo %s nao existe mais no servidor."
                             % item.origem)
    tamanho = info.size
    item.tamanho = tamanho
    retomada = validar_download(parcial, item.bytes_feitos, tamanho,
                                info.mtime, item.mtime,
                                origem.resume_download)
    if item.bytes_feitos and not retomada:
        logger.info("Recomecando %s do zero: %s", item.nome, retomada.motivo)
        try:
            os.remove(parcial)
        except OSError:
            pass
        item.bytes_feitos = 0
    item.mtime = info.mtime
    item.offset_inicial = retomada.offset

    if medidor is not None:
        medidor.comecar(total=max(tamanho - retomada.offset, 0), feitos=0)

    os.makedirs(os.path.dirname(alvo) or ".", exist_ok=True)
    modo = "r+b" if retomada.offset else "wb"
    movidos = 0
    try:
        with open(parcial, modo) as f:
            if retomada.offset:
                f.seek(retomada.offset)
                f.truncate(retomada.offset)
            movidos = origem.baixar(item.origem, f, offset=retomada.offset,
                                    cb=_callback(medidor, store, item,
                                                 limitador),
                                    cancelar=cancelar)
            f.flush()
            os.fsync(f.fileno())
    except Cancelado:
        item.bytes_feitos = retomada.offset + (medidor.feitos if medidor else 0)
        raise
    except Exception:
        item.bytes_feitos = retomada.offset + (medidor.feitos if medidor else 0)
        raise

    os.replace(parcial, alvo)

    ok, detalhe = conferir_integridade(origem, item.origem, alvo, tamanho)
    if not ok:
        # o arquivo errado nao pode ficar no lugar do certo: apaga e deixa o
        # item falhar, para uma nova tentativa comecar do zero
        try:
            os.remove(alvo)
        except OSError:
            pass
        item.bytes_feitos = 0
        raise ErroPermanente("Verificacao falhou: %s." % detalhe)
    logger.debug("%s: %s", item.nome, detalhe)

    if info.mtime:
        try:
            local.definir_mtime(item.destino, info.mtime)
        except ErroRemoto:
            pass
    if medidor is not None:
        medidor.terminar()
    return Resultado(movidos, retomada.offset, bool(item.bytes_feitos
                                                    and not retomada),
                     retomada.motivo)


def _enviar(item, origem: Remote, destino: Remote, medidor, cancelar, store,
            limitador) -> Resultado:
    local = origem if isinstance(origem, LocalRemote) else LocalRemote()
    caminho_local = local.nativo(item.origem)
    if not os.path.exists(caminho_local):
        raise ErroPermanente("O arquivo %s nao esta mais no disco."
                             % item.origem)
    tamanho = os.path.getsize(caminho_local)
    mtime = os.path.getmtime(caminho_local)
    item.tamanho = tamanho

    ja_la = destino.stat(item.destino)
    tamanho_destino = ja_la.size if ja_la is not None else -1
    retomada = validar_upload(tamanho_destino, item.bytes_feitos,
                              destino.resume_upload)
    if item.bytes_feitos and not retomada:
        logger.info("Reenviando %s do zero: %s", item.nome, retomada.motivo)
        item.bytes_feitos = 0
    item.offset_inicial = retomada.offset

    pasta = destino.pai(item.destino)
    if pasta and pasta != "/":
        destino.criar_pastas(pasta)

    if medidor is not None:
        medidor.comecar(total=max(tamanho - retomada.offset, 0), feitos=0)

    try:
        with open(caminho_local, "rb") as f:
            if retomada.offset:
                f.seek(retomada.offset)
            movidos = destino.enviar(f, item.destino, tamanho=tamanho,
                                     offset=retomada.offset,
                                     cb=_callback(medidor, store, item,
                                                  limitador),
                                     cancelar=cancelar)
    except Cancelado:
        item.bytes_feitos = retomada.offset + (medidor.feitos if medidor else 0)
        raise
    except Exception:
        item.bytes_feitos = retomada.offset + (medidor.feitos if medidor else 0)
        raise

    if mtime and destino.preserva_mtime:
        try:
            destino.definir_mtime(item.destino, mtime)
        except ErroRemoto:
            pass
    if medidor is not None:
        medidor.terminar()
    return Resultado(movidos, retomada.offset)


def _entre_servidores(item, origem: Remote, destino: Remote, medidor,
                      cancelar, limitador) -> Resultado:
    """Servidor para servidor, passando pela memoria e nao pelo disco.

    Nao e FXP (transferencia direta entre servidores): FXP esta desligado em
    praticamente todo servidor moderno, por ser um vetor de ataque classico.
    Os bytes passam por aqui, com buffer limitado, e a conta e honesta: a
    velocidade e a do lado mais lento.
    """
    info = origem.stat(item.origem)
    tamanho = info.size if info else -1
    item.tamanho = tamanho
    if medidor is not None:
        medidor.comecar(total=max(tamanho, 0), feitos=0)

    cano = _Cano()
    falha = {}

    def ler():
        try:
            origem.baixar(item.origem, cano, cancelar=cancelar)
        except BaseException as e:      # noqa: BLE001 - repassado abaixo
            falha["erro"] = e
        finally:
            cano.fechar_escrita(falha.get("erro"))

    t = threading.Thread(target=ler, name="ponte-leitura", daemon=True)
    t.start()
    try:
        pasta = destino.pai(item.destino)
        if pasta and pasta != "/":
            destino.criar_pastas(pasta)
        movidos = destino.enviar(cano, item.destino, tamanho=tamanho,
                                 cb=_callback(medidor, None, item, limitador),
                                 cancelar=cancelar)
    finally:
        t.join(timeout=30)
    if "erro" in falha:
        raise falha["erro"]
    if medidor is not None:
        medidor.terminar()
    return Resultado(movidos, 0)


def conferir_integridade(origem: Remote, caminho_remoto: str,
                         caminho_local: str, tamanho_esperado: int = -1):
    """Prova que o que chegou e o que saiu, quando da para provar.

    Conferir tamanho nao detecta bytes trocados de lugar - e esse justamente
    o defeito de uma retomada mal feita. Quando o servidor sabe calcular o
    hash do arquivo dele (XMD5/MD5/XCRC), o hash e comparado. Quando nao
    sabe, sobra o tamanho, e isso e dito com todas as letras em vez de
    fingir que o arquivo foi verificado.

    Devolve (ok: bool, descricao: str).
    """
    real = os.path.getsize(caminho_local)
    if tamanho_esperado >= 0 and real != tamanho_esperado:
        return False, ("o arquivo local tem %d bytes e o servidor informou %d"
                       % (real, tamanho_esperado))
    try:
        remoto = origem.hash_remoto(caminho_remoto)
    except Exception:       # noqa: BLE001 - verificacao nunca derruba o item
        remoto = None
    if not remoto:
        return True, "tamanho conferido (o servidor nao calcula hash)"

    import binascii
    import hashlib
    algoritmo, valor = remoto
    if algoritmo == "md5":
        h = hashlib.md5()
        with open(caminho_local, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                h.update(bloco)
        local = h.hexdigest()
    else:
        crc = 0
        with open(caminho_local, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                crc = binascii.crc32(bloco, crc)
        local = "%08x" % (crc & 0xFFFFFFFF)
    if local.lower() != valor.lower():
        return False, ("o %s do arquivo baixado nao bate com o do servidor"
                       % algoritmo.upper())
    return True, "%s conferido com o servidor" % algoritmo.upper()


def descrever_erro(exc: BaseException) -> str:
    """Mensagem curta para a coluna de erro da fila."""
    texto = str(exc).strip() or type(exc).__name__
    return texto if len(texto) <= 160 else texto[:157] + "..."
