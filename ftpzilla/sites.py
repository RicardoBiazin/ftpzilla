"""Gerente de sites: os servidores salvos.

O arquivo e um JSON legivel de proposito - da para versionar, comparar e
mandar para um colega. O que NAO da para ler nele e senha, passphrase e
refresh token: esses saem protegidos pela conta do Windows (segredos.py).

Quais campos sao segredo nao esta escrito aqui: vem do registro de tipos
(remotes.campos_secretos). Assim um backend novo declara o proprio campo
sensivel e ele passa a ser cifrado sem ninguem lembrar de mexer neste
arquivo - que e exatamente o tipo de esquecimento que vaza credencial.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

from . import log, paths, remotes, segredos

logger = log.get()

#: campos que existem como atributo de Site; o resto vai para 'opcoes'
_ATRIBUTOS = None


@dataclass
class Site:
    """Um servidor salvo."""
    id: str = ""
    nome: str = "Novo site"
    grupo: str = ""                 # pasta do gerente de sites
    kind: str = "ftp"

    host: str = ""
    porta: int = 0                  # 0 = porta padrao do tipo
    usuario: str = ""
    senha: str = ""
    anonimo: bool = False

    # SFTP
    arquivo_chave: str = ""
    passphrase: str = ""
    usar_agente: bool = True

    # FTP/FTPS
    tls_modo: str = "explicito"     # explicito | implicito | nenhum
    cert_fingerprint: str = ""      # certificado fixado pelo usuario
    passivo: bool = True
    encoding: str = "utf-8"

    # navegacao
    pasta_remota: str = ""
    pasta_local: str = ""

    # transferencia
    max_conexoes: int = 4
    limite_kbs: int = 0             # 0 = sem limite

    comentario: str = ""
    opcoes: Dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex

    # --- ajudantes --------------------------------------------------------
    @property
    def porta_efetiva(self) -> int:
        if self.porta:
            return int(self.porta)
        try:
            return remotes.get_spec(self.kind).porta_padrao
        except ValueError:
            return 0

    @property
    def rotulo(self) -> str:
        if self.host:
            return "%s (%s@%s)" % (self.nome, self.usuario or "anonimo", self.host)
        return self.nome

    def get(self, chave: str, padrao=None):
        """Le um campo do site OU de 'opcoes' - o formulario nao precisa
        saber em qual dos dois o backend guardou a informacao."""
        if chave in _atributos():
            return getattr(self, chave)
        return self.opcoes.get(chave, padrao)

    def set(self, chave: str, valor) -> None:
        if chave in _atributos():
            atual = getattr(self, chave)
            if isinstance(atual, bool):
                valor = bool(valor)
            elif isinstance(atual, int) and not isinstance(valor, bool):
                try:
                    valor = int(valor or 0)
                except (TypeError, ValueError):
                    valor = 0
            setattr(self, chave, valor)
        else:
            self.opcoes[chave] = valor

    def copia(self, novo_nome: str = "") -> "Site":
        d = asdict(self)
        d["id"] = uuid.uuid4().hex
        d["nome"] = novo_nome or (self.nome + " (copia)")
        return Site(**d)

    def revelado(self) -> "Site":
        """Copia com os segredos em texto puro, para usar na conexao.

        Devolver uma copia em vez de decifrar no lugar e deliberado: o objeto
        que fica na lista da interface nunca chega a ter a senha em claro.
        """
        d = asdict(self)
        for chave in remotes.campos_secretos(self.kind):
            if chave in d:
                d[chave] = segredos.revelar(d[chave] or "")
            elif chave in d.get("opcoes", {}):
                d["opcoes"][chave] = segredos.revelar(d["opcoes"][chave] or "")
        return Site(**d)


def _atributos() -> set:
    global _ATRIBUTOS
    if _ATRIBUTOS is None:
        _ATRIBUTOS = {f.name for f in fields(Site)} - {"opcoes"}
    return _ATRIBUTOS


class GerenteSites:
    """Lista de sites, com leitura e gravacao do JSON."""

    def __init__(self, caminho: str = ""):
        self.caminho = caminho or paths.arquivo_sites()
        self.sites: List[Site] = []
        self.ordem_colunas: Dict = {}

    # --- disco ------------------------------------------------------------
    def carregar(self) -> "GerenteSites":
        self.sites = []
        if not os.path.exists(self.caminho):
            return self
        try:
            with open(self.caminho, "r", encoding="utf-8") as f:
                dados = json.load(f)
        except (OSError, ValueError) as e:
            logger.error("Nao foi possivel ler %s: %s", self.caminho, e)
            return self
        conhecidos = _atributos()
        for bruto in dados.get("sites", []):
            limpo = {k: v for k, v in bruto.items() if k in conhecidos}
            limpo["opcoes"] = dict(bruto.get("opcoes") or {})
            try:
                self.sites.append(Site(**limpo))
            except TypeError as e:
                logger.warning("Site ignorado no arquivo (%s): %s", e, bruto.get("nome"))
        return self

    def salvar(self) -> None:
        """Grava de forma atomica, com os segredos protegidos."""
        saida = {"versao": 1, "sites": [self._para_json(s) for s in self.sites]}
        tmp = self.caminho + ".tmp"
        os.makedirs(os.path.dirname(self.caminho) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(saida, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.caminho)

    def _para_json(self, site: Site) -> Dict:
        d = asdict(site)
        for chave in remotes.campos_secretos(site.kind):
            escopo = "%s/%s" % (site.id, chave)
            if chave in d:
                d[chave] = segredos.proteger(d[chave] or "", escopo)
            elif chave in d.get("opcoes", {}):
                d["opcoes"][chave] = segredos.proteger(d["opcoes"][chave] or "",
                                                       escopo)
        return d

    # --- manipulacao ------------------------------------------------------
    def adicionar(self, site: Site) -> Site:
        self.sites.append(site)
        return site

    def remover(self, site_id: str) -> bool:
        for i, s in enumerate(self.sites):
            if s.id == site_id:
                # tira do keyring o que ficou guardado fora do JSON
                for chave in remotes.campos_secretos(s.kind):
                    segredos.esquecer(s.get(chave, "") or "")
                del self.sites[i]
                return True
        return False

    def por_id(self, site_id: str) -> Optional[Site]:
        for s in self.sites:
            if s.id == site_id:
                return s
        return None

    def por_nome(self, nome: str) -> Optional[Site]:
        for s in self.sites:
            if s.nome == nome:
                return s
        return None

    def grupos(self) -> List[str]:
        return sorted({s.grupo for s in self.sites if s.grupo})

    def ordenados(self) -> List[Site]:
        return sorted(self.sites, key=lambda s: (s.grupo.casefold(),
                                                 s.nome.casefold()))

    def __len__(self) -> int:
        return len(self.sites)

    def __iter__(self):
        return iter(self.sites)


def site_rapido(kind: str, host: str, usuario: str, senha: str,
                porta: int = 0) -> Site:
    """Site descartavel da barra de conexao rapida (nao vai para o disco)."""
    return Site(nome="Conexao rapida", kind=kind, host=host, usuario=usuario,
                senha=senha, porta=porta, anonimo=not usuario)
