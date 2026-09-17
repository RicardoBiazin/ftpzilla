"""Servidores de teste que sobem dentro do proprio processo.

Testar um cliente FTP contra um servidor de mentira nao prova nada: os bugs
reais estao no protocolo (MLSD x LIST, REST, MFMT, reuso de sessao TLS). Por
isso aqui sobe um servidor FTP de verdade (pyftpdlib) e um SFTP de verdade
(paramiko), em thread, na porta 0 (o sistema escolhe uma livre).

As variacoes existem para exercitar justamente o que costuma faltar num
servidor real - sem REST, sem MFMT, sem MLSD - porque o cliente tem que
descobrir isso pelo FEAT e se adaptar, nao supor.
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import os
import socket
import threading
import time

logging.getLogger("pyftpdlib").setLevel(logging.CRITICAL)
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

USUARIO = "teste"
SENHA = "segredo123"


def tem_pyftpdlib() -> bool:
    try:
        import pyftpdlib  # noqa: F401
        return True
    except ImportError:
        return False


def tem_paramiko() -> bool:
    try:
        import paramiko  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Certificado autoassinado para os testes de FTPS
# ---------------------------------------------------------------------------
def cert_autoassinado(pasta: str) -> str:
    """Gera cert+chave num arquivo PEM unico e devolve o caminho."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    agora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(nome).issuer_name(nome)
            .public_key(chave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - datetime.timedelta(days=1))
            .not_valid_after(agora + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost")]), critical=False)
            .sign(chave, hashes.SHA256()))

    caminho = os.path.join(pasta, "teste.pem")
    with open(caminho, "wb") as f:
        f.write(chave.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return caminho


# ---------------------------------------------------------------------------
# FTP / FTPS
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def servidor_ftp(raiz: str, tls: bool = False, sem_mfmt: bool = False,
                 sem_rest: bool = False, sem_mlsd: bool = False,
                 cortar_em: int = 0, certificado: str = ""):
    """Sobe um servidor FTP sobre 'raiz'. Devolve (host, porta).

    cortar_em > 0 faz a leitura de qualquer arquivo falhar depois de N bytes,
    simulando a queda de conexao no meio de uma transferencia grande - e como
    se testa a retomada sem depender de sorte.
    """
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.filesystems import AbstractedFS
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    class _Cortado:
        """Arquivo que morre depois de N bytes lidos."""

        def __init__(self, fobj, limite):
            self._f = fobj
            self._resta = limite

        def read(self, n=-1):
            if self._resta <= 0:
                raise OSError("conexao interrompida pelo servidor (teste)")
            dados = self._f.read(min(n if n and n > 0 else 8192, self._resta))
            self._resta -= len(dados)
            return dados

        def __getattr__(self, nome):
            return getattr(self._f, nome)

    class FS(AbstractedFS):
        def open(self, filename, mode):
            f = super().open(filename, mode)
            if cortar_em and "r" in mode:
                return _Cortado(f, cortar_em)
            return f

    if tls:
        from pyftpdlib.handlers import TLS_FTPHandler
        base = TLS_FTPHandler
    else:
        base = FTPHandler

    class Handler(base):
        abstracted_fs = FS
        # o timeout padrao de 300s deixaria o teste pendurado se algo travar
        timeout = 20
        banner = "FTPZilla teste"

    Handler.proto_cmds = dict(base.proto_cmds)
    if sem_mfmt:
        Handler.proto_cmds.pop("MFMT", None)
    if sem_rest:
        Handler.proto_cmds.pop("REST", None)
    if sem_mlsd:
        Handler.proto_cmds.pop("MLSD", None)
        Handler.proto_cmds.pop("MLST", None)

    if tls:
        Handler.certfile = certificado or cert_autoassinado(raiz)
        Handler.tls_control_required = False
        Handler.tls_data_required = False

    aut = DummyAuthorizer()
    aut.add_user(USUARIO, SENHA, raiz, perm="elradfmwMT")
    aut.add_anonymous(raiz)
    Handler.authorizer = aut

    servidor = FTPServer(("127.0.0.1", 0), Handler)
    host, porta = servidor.socket.getsockname()[:2]
    t = threading.Thread(target=servidor.serve_forever,
                         kwargs={"timeout": 0.1, "handle_exit": False},
                         daemon=True)
    t.start()
    _esperar_porta(host, porta)
    try:
        yield host, porta
    finally:
        try:
            servidor.close_all()
        except Exception:
            pass
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# SFTP
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def servidor_sftp(raiz: str, usuario: str = USUARIO, senha: str = SENHA,
                  chave_publica=None):
    """Sobe um servidor SFTP real (paramiko) sobre 'raiz'."""
    import paramiko

    chave_host = paramiko.RSAKey.generate(2048)

    class Servidor(paramiko.ServerInterface):
        def check_auth_password(self, user, pwd):
            if user == usuario and pwd == senha:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def check_auth_publickey(self, user, key):
            if chave_publica is not None and user == usuario:
                if key == chave_publica:
                    return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def get_allowed_auths(self, user):
            return "password,publickey"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

    class Handle(paramiko.SFTPHandle):
        def stat(self):
            try:
                return paramiko.SFTPAttributes.from_stat(
                    os.fstat(self.readfile.fileno()))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def chattr(self, attr):
            return paramiko.SFTP_OK

    class SFTP(paramiko.SFTPServerInterface):
        ROOT = raiz

        def _real(self, caminho):
            caminho = caminho.replace("\\", "/")
            partes = [p for p in caminho.split("/") if p not in ("", ".", "..")]
            return os.path.join(self.ROOT, *partes)

        def list_folder(self, path):
            real = self._real(path)
            try:
                out = []
                for nome in os.listdir(real):
                    attr = paramiko.SFTPAttributes.from_stat(
                        os.stat(os.path.join(real, nome)))
                    attr.filename = nome
                    out.append(attr)
                return out
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        lstat = stat

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                binario = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o666)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            if flags & os.O_WRONLY:
                modo = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                modo = "a+b" if flags & os.O_APPEND else "r+b"
            else:
                modo = "rb"
            try:
                f = os.fdopen(binario, modo)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            h = Handle(flags)
            h.filename = real
            h.readfile = f
            h.writefile = f
            return h

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                os.replace(self._real(oldpath), self._real(newpath))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            real = self._real(path)
            try:
                if getattr(attr, "st_mode", None) is not None:
                    os.chmod(real, attr.st_mode)
                if getattr(attr, "st_atime", None) is not None:
                    os.utime(real, (attr.st_atime, attr.st_mtime))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def canonicalize(self, path):
            if not path or path == ".":
                return "/"
            return "/" + "/".join(p for p in path.replace("\\", "/").split("/")
                                  if p not in ("", ".", ".."))

    escuta = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    escuta.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    escuta.bind(("127.0.0.1", 0))
    escuta.listen(8)
    host, porta = escuta.getsockname()[:2]
    parar = threading.Event()
    transportes = []

    def atender():
        while not parar.is_set():
            try:
                escuta.settimeout(0.3)
                conn, _ = escuta.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            try:
                t = paramiko.Transport(conn)
                t.add_server_key(chave_host)
                t.set_subsystem_handler("sftp", paramiko.SFTPServer, SFTP)
                t.start_server(server=Servidor())
                transportes.append(t)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    t = threading.Thread(target=atender, daemon=True)
    t.start()
    try:
        yield host, porta, chave_host
    finally:
        parar.set()
        for tr in transportes:
            try:
                tr.close()
            except Exception:
                pass
        try:
            escuta.close()
        except Exception:
            pass
        t.join(timeout=5)


def _esperar_porta(host: str, porta: int, prazo: float = 5.0) -> None:
    fim = time.time() + prazo
    while time.time() < fim:
        try:
            with socket.create_connection((host, porta), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("o servidor de teste nao subiu em %.0fs" % prazo)
