"""SFTP sobre SSH, com paramiko.

Tres coisas aqui valem mais que o resto do arquivo:

1. VELOCIDADE. SFTP sem prefetch() e uma conversa de pergunta-e-resposta:
   cada bloco espera a viagem de ida e volta. Num link com 80 ms de
   latencia isso da uns 2 MB/s independentemente da banda. Com prefetch no
   download e set_pipelined no upload, o paramiko mantem varios pedidos no
   ar e a mesma conexao passa de dezenas de MB/s. E o maior ganho de
   desempenho do programa inteiro, e sao duas linhas.

2. CHAVE DE HOST. A politica e RejectPolicy: chave desconhecida NAO conecta
   sozinha - ela vira uma pergunta para o usuario, com a impressao digital
   no mesmo formato que o OpenSSH imprime, para dar para comparar. Chave que
   MUDOU e recusada com alarde, porque ou o servidor foi reinstalado ou tem
   alguem no meio do caminho, e so o usuario sabe qual dos dois.

3. CHAVES MODERNAS. PKey.from_path() abre RSA, Ed25519 e ECDSA sem o
   programa ter que adivinhar o tipo. Adivinhar (tentar RSAKey primeiro)
   quebra com as chaves que o ssh-keygen gera por padrao hoje.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import socket
import stat as statmod
from typing import List, Optional

from . import Field, RemoteSpec, register
from .. import log, paths
from .base import (BLOCO, Cancelado, Entry, ErroAutenticacao,
                   ErroChaveDesconhecida, ErroPermanente, ErroRemoto,
                   ErroTransitorio, Remote)

logger = log.get()

PORTA = 22
TIMEOUT = 30
#: quantos bytes o paramiko mantem no ar no download
PREFETCH_MINIMO = 64 * 1024


def _paramiko():
    import paramiko
    return paramiko


def _traduzir(exc: Exception) -> ErroRemoto:
    """Erro do paramiko -> excecao do FTPZilla."""
    if isinstance(exc, ErroRemoto):
        return exc
    paramiko = _paramiko()
    if isinstance(exc, paramiko.AuthenticationException):
        return ErroAutenticacao(str(exc) or "credencial recusada pelo servidor")
    if isinstance(exc, paramiko.PasswordRequiredException):
        return ErroAutenticacao("a chave privada esta protegida por senha")
    if isinstance(exc, IOError):
        # o SFTPClient levanta IOError com errno preenchido
        n = getattr(exc, "errno", None)
        if n in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOTEMPTY,
                 errno.EEXIST, errno.ENOSPC):
            return ErroPermanente(str(exc))
        return ErroTransitorio(str(exc))
    if isinstance(exc, socket.gaierror):
        return ErroTransitorio("servidor nao encontrado (o nome nao resolve "
                               "no DNS); confira o endereco")
    if isinstance(exc, ConnectionRefusedError):
        return ErroTransitorio("conexao recusada: nada escutando nessa porta")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ErroTransitorio("o servidor nao respondeu a tempo")
    if isinstance(exc, (ConnectionError, OSError)):
        return ErroTransitorio(str(exc) or type(exc).__name__)
    if isinstance(exc, paramiko.SSHException):
        return ErroTransitorio(str(exc))
    return ErroRemoto(str(exc))


def impressao(chave) -> str:
    """SHA256:base64, exatamente como o OpenSSH imprime.

    O formato importa: e assim que o administrador do servidor vai ler a
    impressao para voce conferir. Mostrar em hexadecimal obrigaria a pessoa
    a converter na mao, e na pratica ela so clicaria em 'aceitar'.
    """
    digest = hashlib.sha256(chave.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _nome_host(host: str, porta: int) -> str:
    return host if porta in (0, PORTA) else "[%s]:%d" % (host, porta)


def carregar_known_hosts(incluir_do_usuario: bool = True):
    """Le o known_hosts do FTPZilla e, opcionalmente, o do OpenSSH.

    O do usuario e lido, nunca escrito: o FTPZilla nao mexe no arquivo que
    o ssh da pessoa usa.
    """
    paramiko = _paramiko()
    chaves = paramiko.HostKeys()
    proprio = paths.arquivo_known_hosts()
    if os.path.exists(proprio):
        try:
            chaves.load(proprio)
        except Exception as e:      # noqa: BLE001
            logger.warning("known_hosts do FTPZilla ilegivel: %s", e)
    if incluir_do_usuario:
        do_usuario = paths.known_hosts_do_usuario()
        if os.path.exists(do_usuario):
            try:
                outro = paramiko.HostKeys()
                outro.load(do_usuario)
                for nome in outro.keys():
                    for tipo, chave in outro[nome].items():
                        chaves.add(nome, tipo, chave)
            except Exception as e:  # noqa: BLE001
                logger.debug("known_hosts do usuario ilegivel: %s", e)
    return chaves


def gravar_known_host(host: str, porta: int, chave) -> None:
    """Acrescenta a chave ao known_hosts do FTPZilla."""
    paramiko = _paramiko()
    caminho = paths.arquivo_known_hosts()
    chaves = paramiko.HostKeys()
    if os.path.exists(caminho):
        try:
            chaves.load(caminho)
        except Exception:
            pass
    chaves.add(_nome_host(host, porta), chave.get_name(), chave)
    chaves.save(caminho)
    logger.info("Chave de host de %s salva em %s", host, caminho)


class SftpRemote(Remote):
    kind = "sftp"
    rotulo = "SFTP (SSH)"

    resume_download = True
    resume_upload = True
    segmentavel = True
    pode_renomear = True
    pode_chmod = True
    preserva_mtime = True
    case_sensitive = True
    max_conexoes = 4

    def __init__(self, site=None):
        super().__init__(site)
        self.transport = None
        self.sftp = None
        self.suja = False
        self._home = "/"
        self.fingerprint = ""

    # --- conexao ----------------------------------------------------------
    def conectar(self) -> None:
        paramiko = _paramiko()
        site = self.site
        host = getattr(site, "host", "")
        porta = int(getattr(site, "porta_efetiva", 0) or PORTA)
        try:
            sock = socket.create_connection((host, porta), TIMEOUT)
            self.transport = paramiko.Transport(sock)
            self.transport.set_keepalive(30)
            self.transport.start_client(timeout=TIMEOUT)
            self._conferir_chave_host(host, porta)
            self._autenticar()
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
            self.sftp.get_channel().settimeout(TIMEOUT)
            try:
                self._home = self.sftp.normalize(".")
            except Exception:
                self._home = "/"
            self._conectado = True
            self.suja = False
        except ErroRemoto:
            self._fechar_silencioso()
            raise
        except Exception as e:
            self._fechar_silencioso()
            raise _traduzir(e) from e

    def _conferir_chave_host(self, host: str, porta: int) -> None:
        chave = self.transport.get_remote_server_key()
        self.fingerprint = impressao(chave)
        nome = _nome_host(host, porta)

        decisao = (getattr(self.site, "opcoes", {}) or {}).get("aceitar_chave", "")
        if decisao in ("salvar", "uma_vez"):
            # o usuario acabou de responder o dialogo de confianca
            if decisao == "salvar":
                gravar_known_host(host, porta, chave)
            try:
                self.site.opcoes.pop("aceitar_chave", None)
            except Exception:
                pass
            return

        conhecidas = carregar_known_hosts(
            (getattr(self.site, "opcoes", {}) or {}).get("usar_known_hosts_do_ssh",
                                                         True))
        esperadas = conhecidas.lookup(nome)
        if esperadas is None:
            raise ErroChaveDesconhecida(host, self.fingerprint, mudou=False)
        guardada = esperadas.get(chave.get_name())
        if guardada is None:
            # o servidor apresentou um tipo de chave que nunca vimos dele;
            # pode ser so uma chave nova do mesmo servidor, mas quem decide
            # continua sendo o usuario
            raise ErroChaveDesconhecida(host, self.fingerprint, mudou=False)
        if guardada.asbytes() != chave.asbytes():
            raise ErroChaveDesconhecida(host, self.fingerprint, mudou=True)

    def _autenticar(self) -> None:
        paramiko = _paramiko()
        site = self.site
        usuario = getattr(site, "usuario", "") or ""
        senha = getattr(site, "senha", "") or ""
        arquivo = (getattr(site, "arquivo_chave", "") or "").strip()
        frase = getattr(site, "passphrase", "") or ""
        erros = []

        if arquivo:
            try:
                chave = paramiko.PKey.from_path(arquivo,
                                                passphrase=frase or None)
                self.transport.auth_publickey(usuario, chave)
                return
            except Exception as e:      # noqa: BLE001
                erros.append("chave %s: %s" % (os.path.basename(arquivo), e))

        if getattr(site, "usar_agente", True):
            try:
                for chave in paramiko.Agent().get_keys():
                    try:
                        self.transport.auth_publickey(usuario, chave)
                        logger.info("Autenticado por chave do agente SSH.")
                        return
                    except paramiko.SSHException:
                        continue
            except Exception:
                pass

        if senha:
            try:
                self.transport.auth_password(usuario, senha)
                return
            except Exception as e:      # noqa: BLE001
                erros.append(str(e))

        raise ErroAutenticacao("; ".join(erros)
                               or "nenhuma credencial foi aceita pelo servidor")

    def viva(self) -> bool:
        if not self._conectado or self.sftp is None:
            return False
        try:
            self.sftp.stat(".")
            return True
        except Exception:
            self._conectado = False
            return False

    def clone(self) -> "SftpRemote":
        return SftpRemote(self.site)

    def _fechar_silencioso(self) -> None:
        for obj in (self.sftp, self.transport):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.sftp = None
        self.transport = None

    def fechar(self) -> None:
        self._fechar_silencioso()
        self._conectado = False

    # --- caminhos ---------------------------------------------------------
    def home(self) -> str:
        inicial = getattr(self.site, "pasta_remota", "") or ""
        return self.normalizar(inicial or self._home or "/")

    # --- navegacao --------------------------------------------------------
    def listar(self, caminho: str) -> List[Entry]:
        caminho = self.normalizar(caminho)
        try:
            brutos = self.sftp.listdir_attr(caminho)
        except Exception as e:
            raise _traduzir(e) from e
        out = []
        for a in brutos:
            if a.filename in (".", ".."):
                continue
            out.append(self._entry(a))
        return out

    def _entry(self, a, nome: str = "") -> Entry:
        modo = a.st_mode or 0
        eh_link = statmod.S_ISLNK(modo)
        eh_dir = statmod.S_ISDIR(modo)
        return Entry(
            name=nome or getattr(a, "filename", ""),
            is_dir=eh_dir,
            size=0 if eh_dir else int(a.st_size or 0),
            mtime=float(a.st_mtime or 0),
            perms=statmod.filemode(modo)[1:] if modo else "",
            owner=str(a.st_uid) if a.st_uid is not None else "",
            group=str(a.st_gid) if a.st_gid is not None else "",
            is_link=eh_link,
        )

    def stat(self, caminho: str) -> Optional[Entry]:
        caminho = self.normalizar(caminho)
        try:
            a = self.sftp.stat(caminho)
        except IOError:
            return None
        except Exception as e:
            raise _traduzir(e) from e
        return self._entry(a, self.nome(caminho) or caminho)

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        try:
            self.sftp.mkdir(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def apagar_arquivo(self, caminho: str) -> None:
        try:
            self.sftp.remove(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def apagar_pasta(self, caminho: str) -> None:
        try:
            self.sftp.rmdir(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def renomear(self, de: str, para: str) -> None:
        try:
            self.sftp.posix_rename(self.normalizar(de), self.normalizar(para))
        except (IOError, AttributeError):
            # posix_rename e uma extensao; nem todo servidor tem
            try:
                self.sftp.rename(self.normalizar(de), self.normalizar(para))
            except Exception as e:
                raise _traduzir(e) from e
        except Exception as e:
            raise _traduzir(e) from e

    def chmod(self, caminho: str, modo: int) -> None:
        try:
            self.sftp.chmod(self.normalizar(caminho), modo)
        except Exception as e:
            raise _traduzir(e) from e

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        if not mtime:
            return
        try:
            self.sftp.utime(self.normalizar(caminho), (mtime, mtime))
        except Exception as e:
            logger.info("Nao deu para gravar a data de %s: %s", caminho, e)
            self.preserva_mtime = False

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite=None, cb=None, cancelar=None) -> int:
        caminho = self.normalizar(caminho)
        escritos = 0
        restante = limite
        try:
            with self.sftp.open(caminho, "rb") as f:
                tamanho = 0
                try:
                    tamanho = f.stat().st_size
                except Exception:
                    tamanho = 0
                # prefetch e a diferenca entre alguns MB/s e dezenas de MB/s
                # em link com latencia: sem ele, cada bloco espera a ida e
                # volta ate o servidor
                a_ler = (restante if restante is not None
                         else max(tamanho - offset, 0))
                if a_ler > PREFETCH_MINIMO:
                    try:
                        f.prefetch(a_ler)
                    except Exception:
                        pass
                if offset:
                    f.seek(offset)
                while True:
                    pedir = BLOCO if restante is None else min(BLOCO, restante)
                    if pedir <= 0:
                        break
                    dados = f.read(pedir)
                    if not dados:
                        break
                    destino_fobj.write(dados)
                    escritos += len(dados)
                    if restante is not None:
                        restante -= len(dados)
                    self._passo(cb, len(dados), cancelar)
        except Cancelado:
            self.suja = True
            raise
        except Exception as e:
            self.suja = True
            raise _traduzir(e) from e
        return escritos

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb=None, cancelar=None) -> int:
        caminho = self.normalizar(caminho)
        enviados = 0
        modo = "r+b" if offset else "wb"
        try:
            with self.sftp.open(caminho, modo) as f:
                # pipelined: o paramiko para de esperar a confirmacao de cada
                # escrita antes de mandar a proxima
                f.set_pipelined(True)
                if offset:
                    f.seek(offset)
                while True:
                    bloco = origem_fobj.read(BLOCO)
                    if not bloco:
                        break
                    f.write(bloco)
                    enviados += len(bloco)
                    self._passo(cb, len(bloco), cancelar)
        except Cancelado:
            self.suja = True
            raise
        except Exception as e:
            self.suja = True
            raise _traduzir(e) from e
        return enviados


register(RemoteSpec(
    kind="sftp",
    label="SFTP (SSH)",
    factory=lambda site=None: SftpRemote(site),
    porta_padrao=PORTA,
    icone="servidor",
    requires=["paramiko"],
    fields=[
        Field("host", "Servidor", required=True),
        Field("porta", "Porta", kind="int", default=0, width=8,
              help="0 usa a porta 22."),
        Field("usuario", "Usuario", required=True),
        Field("senha", "Senha", kind="password",
              help="Deixe vazio para usar so a chave ou o agente SSH."),
        Field("arquivo_chave", "Arquivo de chave", kind="file",
              help="Chave privada (RSA, Ed25519 ou ECDSA)."),
        Field("passphrase", "Senha da chave", kind="password"),
        Field("usar_agente", "Usar o agente SSH", kind="bool", default=True,
              help="Aproveita as chaves ja carregadas no Pageant ou ssh-agent."),
        Field("pasta_remota", "Pasta inicial"),
        Field("max_conexoes", "Conexoes simultaneas", kind="int", default=4,
              width=8),
    ],
    note="A chave do servidor e conferida a cada conexao. Na primeira vez, "
         "compare a impressao digital com a que o administrador informou.",
))
