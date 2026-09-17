"""Protecao das credenciais gravadas em disco.

Usa a DPAPI do Windows (CryptProtectData): a chave fica amarrada a conta de
usuario do Windows, entao o sites.json copiado para outra maquina - ou lido
por outro usuario - nao entrega as senhas. Nao ha chave mestra para guardar
em lugar nenhum, que e justamente a vantagem sobre inventar uma cifra com
senha propria.

Isso importa mais aqui do que num app comum: um refresh token de OAuth vale
mais que uma senha, porque da acesso continuo a conta ate ser revogado, e a
senha de FTP costuma ser a mesma do painel de hospedagem.

Ha um backend alternativo, 'keyring', para quem usa perfil movel ou quer ver
as credenciais no Gerenciador de Credenciais do Windows. Ele e opcional de
verdade: se o pacote nao estiver instalado, o modulo nem tenta.

Fora do Windows, sem DPAPI e sem keyring, o valor fica em TEXTO PURO e
backend_atual() devolve 'texto' - a interface mostra isso como aviso na
tela, e nao so no log, porque e uma diferenca que o usuario precisa saber.
"""
from __future__ import annotations

import base64
import logging

PREFIXO_DPAPI = "dpapi:"
PREFIXO_KEYRING = "keyring:"
SERVICO_KEYRING = "FTPZilla"

logger = logging.getLogger("ftpzilla")

#: 'auto' escolhe DPAPI; 'keyring' forca o Gerenciador de Credenciais
_preferencia = ["auto"]
_cache = []          # [api ou None] - resolvido uma vez so


def preferir(backend: str) -> None:
    """Define o backend: 'auto' (DPAPI) ou 'keyring'."""
    _preferencia[0] = backend if backend in ("auto", "keyring") else "auto"


def _api():
    if _cache:
        return _cache[0]
    api = None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt = ctypes.windll.crypt32
        kernel = ctypes.windll.kernel32

        def _blob(dados: bytes) -> DATA_BLOB:
            buf = ctypes.create_string_buffer(dados, len(dados))
            return DATA_BLOB(len(dados),
                             ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

        def _bytes(blob: DATA_BLOB) -> bytes:
            return ctypes.string_at(blob.pbData, blob.cbData)

        def _liberar(blob: DATA_BLOB) -> None:
            if blob.pbData:
                kernel.LocalFree(blob.pbData)

        api = (ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar)
    except Exception:
        api = None
    _cache.append(api)
    return api


def _keyring():
    if _preferencia[0] != "keyring":
        return None
    try:
        import keyring
        return keyring
    except Exception:
        return None


def disponivel() -> bool:
    """Ha alguma protecao real disponivel nesta maquina?"""
    return backend_atual() != "texto"


def backend_atual() -> str:
    """'dpapi', 'keyring' ou 'texto'."""
    if _keyring() is not None:
        return "keyring"
    return "dpapi" if _api() is not None else "texto"


def proteger(valor: str, escopo: str = "") -> str:
    """Texto puro -> valor protegido. Devolve o proprio valor se nao der.

    'escopo' e o identificador usado quando o backend guarda o segredo fora
    do arquivo (keyring); com DPAPI ele e ignorado, porque o valor cifrado
    vive dentro do proprio JSON.
    """
    if not valor or protegido(valor):
        return valor

    kr = _keyring()
    if kr is not None and escopo:
        try:
            kr.set_password(SERVICO_KEYRING, escopo, valor)
            return PREFIXO_KEYRING + escopo
        except Exception as e:
            logger.warning("Nao foi possivel usar o keyring (%s); caindo para DPAPI.", e)

    api = _api()
    if api is None:
        logger.warning("Sem DPAPI nesta maquina: a credencial ficara em texto puro.")
        return valor
    ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar = api
    entrada = _blob(valor.encode("utf-8"))
    saida = DATA_BLOB()
    ok = crypt.CryptProtectData(ctypes.byref(entrada), None, None, None, None,
                                0, ctypes.byref(saida))
    if not ok:
        logger.warning("Nao foi possivel proteger a credencial (DPAPI).")
        return valor
    try:
        return PREFIXO_DPAPI + base64.b64encode(_bytes(saida)).decode("ascii")
    finally:
        _liberar(saida)


def revelar(valor: str) -> str:
    """Valor protegido -> texto puro. Valor sem prefixo volta como esta."""
    if not valor:
        return valor

    if valor.startswith(PREFIXO_KEYRING):
        try:
            import keyring
            return keyring.get_password(SERVICO_KEYRING,
                                        valor[len(PREFIXO_KEYRING):]) or ""
        except Exception as e:
            logger.warning("Credencial no keyring nao pode ser lida: %s", e)
            return ""

    if not valor.startswith(PREFIXO_DPAPI):
        return valor

    api = _api()
    if api is None:
        logger.warning("Credencial protegida, mas a DPAPI nao esta disponivel.")
        return ""
    ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar = api
    try:
        bruto = base64.b64decode(valor[len(PREFIXO_DPAPI):])
    except Exception:
        return ""
    entrada = _blob(bruto)
    saida = DATA_BLOB()
    ok = crypt.CryptUnprotectData(ctypes.byref(entrada), None, None, None,
                                  None, 0, ctypes.byref(saida))
    if not ok:
        # tipico de configuracao copiada de outra maquina ou de outro usuario
        logger.warning("Credencial gravada por outro usuario ou outra maquina: "
                       "sera preciso informa-la de novo.")
        return ""
    try:
        return _bytes(saida).decode("utf-8", "replace")
    finally:
        _liberar(saida)


def protegido(valor: str) -> bool:
    return bool(valor) and (valor.startswith(PREFIXO_DPAPI)
                            or valor.startswith(PREFIXO_KEYRING))


def esquecer(valor: str) -> None:
    """Remove do keyring o segredo apontado por 'valor' (ao apagar um site)."""
    if valor and valor.startswith(PREFIXO_KEYRING):
        try:
            import keyring
            keyring.delete_password(SERVICO_KEYRING, valor[len(PREFIXO_KEYRING):])
        except Exception:
            pass


def aviso_se_desprotegido() -> str:
    """Texto do aviso a mostrar na interface, ou '' se estiver tudo certo."""
    if backend_atual() == "texto":
        return ("Esta maquina nao tem a protecao de credenciais do Windows: "
                "as senhas serao gravadas em texto puro.")
    return ""
