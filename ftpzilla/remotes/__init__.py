"""Registro de tipos de conexao.

Cada backend se registra com um RemoteSpec que descreve os campos de conexao.
A GUI monta o formulario do gerente de sites a partir dessa descricao, entao
um protocolo novo aparece na interface sozinho - ninguem precisa mexer em
ui/dialogos.py. O mesmo registro diz quais campos sao segredo (e devem ser
gravados cifrados) e quais pacotes o tipo precisa.

Os imports dos backends ficam no FIM do arquivo, de proposito: eles importam
nomes daqui, entao registrar antes de definir causaria import circular.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dcfield
from typing import Callable, Dict, List

from .base import (BLOCO, Cancelado, Entry, ErroAutenticacao, ErroCertificado,
                   ErroChaveDesconhecida, ErroPermanente, ErroRemoto,
                   ErroTransitorio, Remote)

__all__ = [
    "BLOCO", "Cancelado", "Entry", "ErroAutenticacao", "ErroCertificado",
    "ErroChaveDesconhecida", "ErroPermanente", "ErroRemoto", "ErroTransitorio",
    "Remote", "Field", "RemoteSpec", "register", "get_spec", "kinds",
    "campos", "campos_secretos", "missing_requirements", "make_remote",
    "FIELD_KINDS", "FIELD_KINDS_SECRETOS",
]

#: tipos de widget que a GUI sabe desenhar
FIELD_KINDS = ("text", "password", "int", "bool", "file", "dir", "escolha", "oauth")
#: campos cujo valor nunca vai para o disco em texto puro
FIELD_KINDS_SECRETOS = ("password", "oauth")


@dataclass
class Field:
    """Um campo de conexao, renderizado automaticamente pela GUI.

    'key' que corresponda a um atributo de sites.Site e gravado nele; qualquer
    outro nome vai para Site.opcoes (dicionario livre), que e onde os tipos
    novos guardam o que so eles entendem.
    """
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    required: bool = False
    default: object = ""
    width: int = 24
    #: so para kind='escolha': lista de (valor, rotulo)
    opcoes: List[tuple] = dcfield(default_factory=list)
    #: so para kind='oauth': nome do provedor em oauth.PROVEDORES
    provedor: str = ""

    @property
    def secreto(self) -> bool:
        return self.kind in FIELD_KINDS_SECRETOS


@dataclass
class RemoteSpec:
    kind: str                                   # id usado no sites.json
    label: str                                  # texto na interface
    factory: Callable[..., Remote]              # (site) -> Remote
    fields: List[Field] = dcfield(default_factory=list)
    porta_padrao: int = 0
    icone: str = "servidor"
    #: modulos necessarios. "modulo" ou "modulo:nome-no-pip" quando os dois
    #: nomes diferem (ex.: "paramiko" x "azure.storage.blob:azure-storage-blob")
    requires: List[str] = dcfield(default_factory=list)
    note: str = ""


_SPECS: "Dict[str, RemoteSpec]" = {}


def register(spec: RemoteSpec) -> RemoteSpec:
    _SPECS[spec.kind] = spec
    return spec


def get_spec(kind: str) -> RemoteSpec:
    try:
        return _SPECS[kind]
    except KeyError:
        raise ValueError("Tipo de conexao desconhecido: %r" % kind)


def kinds() -> List[str]:
    return list(_SPECS)


def campos(kind: str) -> List[Field]:
    return get_spec(kind).fields if kind in _SPECS else []


def campos_secretos(kind: str) -> List[str]:
    """Chaves cujo valor deve ser gravado cifrado."""
    if kind not in _SPECS:
        return ["senha", "passphrase"]   # tipo desconhecido: protege o obvio
    return [f.key for f in _SPECS[kind].fields if f.secreto]


def missing_requirements(kind: str) -> List[str]:
    """Pacotes declarados pelo tipo que nao estao instalados.

    'requires' lista o modulo; o retorno e o nome de INSTALACAO, que e o que
    interessa a quem vai rodar o pip. Assim uma dependencia opcional ausente
    vira um aviso amigavel na interface, e nao um ImportError no meio de uma
    transferencia.
    """
    import importlib.util
    out = []
    for item in get_spec(kind).requires:
        mod, _, pip_name = item.partition(":")
        try:
            achou = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError, AttributeError):
            achou = False
        if not achou:
            out.append(pip_name or mod.replace(".", "-"))
    return out


def make_remote(site) -> Remote:
    """Cria (sem conectar) o Remote do tipo declarado no site."""
    spec = get_spec(getattr(site, "kind", "local"))
    faltando = missing_requirements(spec.kind)
    if faltando:
        raise RuntimeError(
            "%s precisa dos pacotes: %s (pip install %s)"
            % (spec.label, ", ".join(faltando), " ".join(faltando)))
    return spec.factory(site)


def rotulo(kind: str) -> str:
    return _SPECS[kind].label if kind in _SPECS else kind


# --- registros: no fim, porque os backends importam nomes deste modulo -----
from . import local as _local        # noqa: E402,F401
from . import ftp as _ftp            # noqa: E402,F401
from . import sftp as _sftp          # noqa: E402,F401
