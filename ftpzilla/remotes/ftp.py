"""FTP, FTPS explicito e FTPS implicito.

Usa o ftplib da biblioteca padrao de proposito. Nenhuma biblioteca de
terceiros expoe o que este programa precisa: transfercmd(cmd, rest=...), que
e o unico jeito de ler do socket de dados em blocos, com callback de
progresso, faixa limitada e retomada por offset. retrbinary() nao serve -
ele nao aceita rest e so devolve o controle no fim.

Tres armadilhas resolvidas aqui, todas classicas:

1. REUSO DE SESSAO TLS. Servidor endurecido (vsftpd com require_ssl_reuse,
   que e o PADRAO) exige que o canal de dados reaproveite a sessao TLS do
   canal de controle. O ftplib nao faz isso sozinho, e o sintoma e uma
   transferencia que morre com "connection reset" sem explicacao nenhuma.
   _FTPTLS.ntransfercmd resolve.

2. FTPS IMPLICITO (porta 990). O ftplib so conhece o explicito. A saida e
   trocar o setter de 'sock' para embrulhar o socket em TLS assim que ele
   aparece, antes de qualquer byte de protocolo.

3. CAPACIDADE DESCOBERTA, NAO SUPOSTA. REST, MFMT, MLSD e SITE CHMOD sao
   lidos do FEAT na conexao e viram atributos da INSTANCIA. Supor que existem
   quebra em servidor antigo; supor que nao existem desliga a retomada em
   todo mundo.
"""
from __future__ import annotations

import calendar
import ftplib
import hashlib
import re
import socket
import ssl
import time
from typing import List, Optional

from . import Field, RemoteSpec, register
from .. import log
from .base import (BLOCO, Cancelado, Entry, ErroAutenticacao, ErroCertificado,
                   ErroPermanente, ErroRemoto, ErroTransitorio, Remote)

logger = log.get()

TIMEOUT = 30
#: prazo para o PRIMEIRO byte chegar no canal de dados. Servidor sobrecarregado
#: aceita a conexao de dados e nunca manda nada; sem este prazo, quem espera e
#: o usuario - ate o servidor resetar do lado dele, o que pode levar 20s ou
#: mais. Desistir antes e tentar outra conexao costuma ser instantaneo.
TIMEOUT_PRIMEIRO_BYTE = 12
#: prazo entre blocos, ja com a transferencia andando
TIMEOUT_DADOS = 60
PORTA = 21
PORTA_IMPLICITA = 990

#: servidores cujas limitacoes ja foram anunciadas nesta sessao. Sem isto, o
#: log repete "nao aceita MFMT" a cada conexao nova - e um download em quatro
#: faixas abre quatro - escondendo o que interessa.
_JA_AVISADO = set()

#: respostas que nao adianta repetir
_PERMANENTES = ("530", "550", "552", "553")
#: respostas que pedem nova tentativa
_TRANSITORIOS = ("421", "425", "426", "434", "450", "451", "452")


def _traduzir(exc: Exception) -> ErroRemoto:
    """Erro do ftplib -> excecao do FTPZilla.

    E esta funcao que permite ao transfer.py decidir entre repetir, pedir
    senha de novo ou desistir sem nunca importar ftplib.
    """
    if isinstance(exc, ErroRemoto):
        return exc
    if isinstance(exc, ftplib.error_perm):
        codigo = str(exc)[:3]
        if codigo in ("530", "532"):
            return ErroAutenticacao(str(exc))
        return ErroPermanente(str(exc))
    if isinstance(exc, ftplib.error_temp):
        codigo = str(exc)[:3]
        if codigo == "421":
            return ErroTransitorio(str(exc))
        return ErroTransitorio(str(exc))
    if isinstance(exc, socket.gaierror):
        # "[Errno 11001] getaddrinfo failed" nao diz nada a quem digitou o
        # endereco errado - e esse e o caso quase sempre
        return ErroTransitorio("servidor nao encontrado (o nome nao resolve "
                               "no DNS); confira o endereco")
    if isinstance(exc, ConnectionRefusedError):
        return ErroTransitorio("conexao recusada: o servidor respondeu, mas "
                               "nao ha nada escutando nessa porta")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ErroTransitorio("o servidor nao respondeu a tempo (pode ser "
                               "firewall, ou a porta errada)")
    if isinstance(exc, (ConnectionError, OSError)):
        return ErroTransitorio(str(exc) or type(exc).__name__)
    if isinstance(exc, ftplib.all_errors):
        texto = str(exc)
        if texto[:3] in _PERMANENTES:
            return ErroPermanente(texto)
        if texto[:3] in _TRANSITORIOS:
            return ErroTransitorio(texto)
        return ErroTransitorio(texto)
    return ErroRemoto(str(exc))


# ---------------------------------------------------------------------------
# Os dois ajustes no ftplib
# ---------------------------------------------------------------------------
class _FTPTLS(ftplib.FTP_TLS):
    """FTP_TLS que reaproveita a sessao TLS no canal de dados."""

    def ntransfercmd(self, cmd, rest=None):
        conn, tamanho = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            sessao = getattr(self.sock, "session", None)
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=sessao)
        return conn, tamanho


class _FTPTLSImplicito(_FTPTLS):
    """FTPS implicito: TLS desde o primeiro byte, sem AUTH TLS.

    O ftplib nao tem suporte a isso. O truque e interceptar a atribuicao de
    'sock' - e o unico ponto por onde o socket cru passa antes de o protocolo
    comecar.
    """

    def __init__(self, *args, **kwargs):
        self._sock = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, valor):
        if valor is not None and not isinstance(valor, ssl.SSLSocket):
            valor = self.context.wrap_socket(valor, server_hostname=self.host)
        self._sock = valor


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------
_MESES = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

_RE_UNIX = re.compile(
    r"^(?P<perm>[bcdlpsSt-][rwxsStT-]{9})[+@.]?\s+"
    r"(?P<links>\d+)\s+"
    r"(?P<dono>\S+)\s+"
    r"(?P<grupo>\S+)\s+"
    r"(?P<tam>\d+)\s+"
    r"(?P<mes>[A-Za-z]{3})\s+"
    r"(?P<dia>\d{1,2})\s+"
    r"(?P<hora>[\d:]{4,5})\s+"
    r"(?P<nome>.+)$")

_RE_DOS = re.compile(
    r"^(?P<data>\d{2}-\d{2}-\d{2,4})\s+"
    r"(?P<hora>\d{2}:\d{2}(?:[AP]M)?)\s+"
    r"(?:(?P<dir><DIR>)|(?P<tam>\d+))\s+"
    r"(?P<nome>.+)$")


def _mtime_mlsd(texto: str) -> float:
    """'20240131235959' (UTC) -> epoch. O FTP fala UTC no MLSD e no MDTM."""
    if not texto:
        return 0.0
    texto = texto.split(".")[0]
    try:
        t = time.strptime(texto[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return 0.0
    return float(calendar.timegm(t))


def _mtime_unix(mes: str, dia: str, hora: str) -> float:
    """A listagem Unix nao tem ano: quando ha hora, o ano e o corrente (ou o
    anterior, se a data ainda nao chegou). E aproximado por natureza - por
    isso a comparacao por data usa tolerancia."""
    m = _MESES.get(mes.lower())
    if not m:
        return 0.0
    agora = time.localtime()
    try:
        if ":" in hora:
            hh, mm = hora.split(":")
            ano = agora.tm_year
            palpite = time.struct_time((ano, m, int(dia), int(hh), int(mm), 0,
                                        0, 1, -1))
            quando = time.mktime(palpite)
            if quando > time.time() + 86400:
                quando = time.mktime(time.struct_time(
                    (ano - 1, m, int(dia), int(hh), int(mm), 0, 0, 1, -1)))
            return float(quando)
        return float(time.mktime(time.struct_time(
            (int(hora), m, int(dia), 0, 0, 0, 0, 1, -1))))
    except (ValueError, OverflowError):
        return 0.0


def parse_linha_lista(linha: str) -> Optional[Entry]:
    """Uma linha de LIST -> Entry. None quando nao da para entender.

    LIST nao e padronizado: cada servidor inventa um formato. Cobrimos os
    dois que existem na pratica (Unix e Windows/IIS) e devolvemos None no
    resto, em vez de inventar um Entry errado - dado errado aqui viraria
    transferencia errada depois.
    """
    linha = linha.rstrip("\r\n")
    if not linha or linha.lower().startswith("total "):
        return None

    m = _RE_UNIX.match(linha)
    if m:
        nome = m.group("nome")
        eh_link = linha[0] == "l"
        alvo = ""
        if eh_link and " -> " in nome:
            nome, alvo = nome.split(" -> ", 1)
        if nome in (".", ".."):
            return None
        return Entry(
            name=nome,
            is_dir=linha[0] == "d",
            size=0 if linha[0] == "d" else int(m.group("tam")),
            mtime=_mtime_unix(m.group("mes"), m.group("dia"), m.group("hora")),
            perms=m.group("perm")[1:],
            owner=m.group("dono"),
            group=m.group("grupo"),
            is_link=eh_link,
            link_target=alvo,
        )

    m = _RE_DOS.match(linha)
    if m:
        nome = m.group("nome")
        if nome in (".", ".."):
            return None
        eh_dir = bool(m.group("dir"))
        quando = 0.0
        try:
            texto = m.group("data") + " " + m.group("hora")
            formato = "%m-%d-%y %I:%M%p" if m.group("hora")[-2:] in ("AM", "PM") \
                else "%m-%d-%y %H:%M"
            if len(m.group("data")) == 10:
                formato = formato.replace("%y", "%Y")
            quando = float(time.mktime(time.strptime(texto, formato)))
        except (ValueError, OverflowError):
            quando = 0.0
        return Entry(name=nome, is_dir=eh_dir,
                     size=0 if eh_dir else int(m.group("tam") or 0),
                     mtime=quando)
    return None


# ---------------------------------------------------------------------------
# O backend
# ---------------------------------------------------------------------------
class FtpRemote(Remote):
    kind = "ftp"
    rotulo = "FTP / FTPS"

    pode_renomear = True
    max_conexoes = 4
    case_sensitive = True

    def __init__(self, site=None):
        super().__init__(site)
        self.ftp: Optional[ftplib.FTP] = None
        self.suja = False            # conexao que precisa ser descartada
        self._home = "/"
        self._feat: List[str] = []
        self._offset_lista: Optional[float] = None   # ver _calibrar_lista
        self.tem_mlsd = False
        self.tem_mdtm = False
        self.tem_mfmt = False
        self.tem_size = True
        self.tem_chmod = False
        # capacidades comecam conservadoras; o FEAT eleva
        self.resume_download = False
        self.resume_upload = False
        self.segmentavel = False
        self.preserva_mtime = False
        self.pode_chmod = False
        self.cert_fingerprint = ""   # o que o servidor apresentou

    # --- conexao ----------------------------------------------------------
    def _contexto_tls(self) -> ssl.SSLContext:
        fixado = (getattr(self.site, "cert_fingerprint", "") or "").strip()
        ctx = ssl.create_default_context()
        if fixado:
            # certificado fixado pelo usuario (tipico de NAS autoassinado):
            # a validacao de cadeia sai, mas a identidade continua checada -
            # so que contra ESTE certificado, e nao contra "qualquer um"
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            try:
                ctx.load_default_certs()
            except Exception:
                pass
        return ctx

    def _conferir_certificado(self) -> None:
        sock = getattr(self.ftp, "sock", None)
        if not isinstance(sock, ssl.SSLSocket):
            return
        try:
            bruto = sock.getpeercert(binary_form=True)
        except Exception:
            return
        if not bruto:
            return
        impressao = hashlib.sha256(bruto).hexdigest().upper()
        self.cert_fingerprint = ":".join(impressao[i:i + 2]
                                         for i in range(0, len(impressao), 2))
        fixado = (getattr(self.site, "cert_fingerprint", "") or "").strip().upper()
        if fixado and fixado.replace(":", "") != impressao:
            raise ErroCertificado(
                getattr(self.site, "host", ""), self.cert_fingerprint,
                "o certificado do servidor mudou desde que voce o aceitou")

    def conectar(self) -> None:
        site = self.site
        modo = (getattr(site, "tls_modo", "explicito") or "explicito").lower()
        host = getattr(site, "host", "")
        porta = int(getattr(site, "porta_efetiva", 0) or
                    (PORTA_IMPLICITA if modo == "implicito" else PORTA))
        encoding = getattr(site, "encoding", "utf-8") or "utf-8"

        try:
            if modo == "nenhum":
                self.ftp = ftplib.FTP(encoding=encoding)
            elif modo == "implicito":
                self.ftp = _FTPTLSImplicito(context=self._contexto_tls(),
                                            encoding=encoding)
            else:
                self.ftp = _FTPTLS(context=self._contexto_tls(),
                                   encoding=encoding)

            self.ftp.connect(host, porta, timeout=TIMEOUT)
            if modo == "explicito":
                self.ftp.auth()
            self._conferir_certificado()

            if getattr(site, "anonimo", False) or not getattr(site, "usuario", ""):
                self.ftp.login()
            else:
                self.ftp.login(site.usuario, getattr(site, "senha", "") or "")

            if modo != "nenhum":
                # PROT P: sem isto o TLS protege so o login, e os arquivos
                # trafegam em claro - o erro de configuracao mais comum
                self.ftp.prot_p()

            self.ftp.set_pasv(bool(getattr(site, "passivo", True)))
            self._ler_capacidades()
            try:
                self._home = self.ftp.pwd() or "/"
            except ftplib.all_errors:
                self._home = "/"
            self._conectado = True
            self.suja = False
        except ssl.SSLCertVerificationError as e:
            self._fechar_silencioso()
            raise ErroCertificado(host, "", str(e)) from e
        except Exception as e:
            self._fechar_silencioso()
            raise _traduzir(e) from e

    def _ler_capacidades(self) -> None:
        """FEAT: descobre o que este servidor aceita, em vez de supor."""
        self._feat = []
        try:
            resposta = self.ftp.sendcmd("FEAT")
            self._feat = [l.strip().upper()
                          for l in resposta.splitlines()[1:-1]]
        except ftplib.all_errors:
            self._feat = []

        texto = " ".join(self._feat)
        # o FEAT anuncia MLST (com a lista de fatos); MLSD vem junto, e nao
        # aparece na lista - procurar so por "MLSD" desligaria a listagem boa
        # em praticamente todo servidor moderno
        self.tem_mlsd = "MLST" in texto or "MLSD" in texto
        self.tem_mdtm = "MDTM" in texto
        self.tem_mfmt = "MFMT" in texto
        self.tem_size = "SIZE" in texto or not self._feat
        self.tem_chmod = "CHMOD" in texto
        tem_rest = "REST STREAM" in texto

        self.resume_download = tem_rest
        self.resume_upload = tem_rest       # confirmado no primeiro uso
        self.segmentavel = tem_rest
        self.preserva_mtime = self.tem_mfmt
        self.pode_chmod = self.tem_chmod

        chave = (getattr(self.site, "host", ""), tem_rest, self.tem_mfmt)
        if self._feat and chave not in _JA_AVISADO:
            _JA_AVISADO.add(chave)
            if not tem_rest:
                logger.info("O servidor nao anuncia REST: transferencia "
                            "interrompida tera de recomecar do zero.")
            if not self.tem_mfmt:
                logger.info("O servidor nao aceita MFMT: a data dos arquivos "
                            "enviados nao sera preservada.")

    def viva(self) -> bool:
        if not self._conectado or self.ftp is None:
            return False
        try:
            self.ftp.voidcmd("NOOP")
            return True
        except Exception:
            self._conectado = False
            return False

    def clone(self) -> "FtpRemote":
        return FtpRemote(self.site)

    def _fechar_silencioso(self) -> None:
        try:
            if self.ftp is not None:
                self.ftp.close()
        except Exception:
            pass
        self.ftp = None

    def fechar(self) -> None:
        try:
            if self.ftp is not None and self._conectado and not self.suja:
                self.ftp.quit()
        except Exception:
            pass
        finally:
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
            if self.tem_mlsd:
                return self._listar_mlsd(caminho)
            return self._listar_list(caminho)
        except ftplib.error_perm as e:
            if self.tem_mlsd and str(e)[:3] in ("500", "502", "550"):
                # servidor anunciou MLSD e nao entrega: rebaixa e tenta LIST
                logger.info("MLSD recusado pelo servidor; usando LIST.")
                self.tem_mlsd = False
                return self._listar_list(caminho)
            raise _traduzir(e) from e
        except Exception as e:
            raise _traduzir(e) from e

    def _listar_mlsd(self, caminho: str) -> List[Entry]:
        out = []
        for nome, fatos in self.ftp.mlsd(caminho,
                                         facts=["type", "size", "modify",
                                                "perm", "unix.mode", "unique"]):
            tipo = (fatos.get("type") or "").lower()
            if tipo in ("cdir", "pdir") or nome in (".", ".."):
                continue
            eh_dir = tipo in ("dir", "cdir", "pdir")
            modo = fatos.get("unix.mode") or ""
            out.append(Entry(
                name=nome,
                is_dir=eh_dir,
                size=0 if eh_dir else int(fatos.get("size") or 0),
                mtime=_mtime_mlsd(fatos.get("modify", "")),
                perms=modo,
                etag=fatos.get("unique", ""),
                is_link=tipo == "oslink",
            ))
        return out

    def _listar_list(self, caminho: str) -> List[Entry]:
        linhas: List[str] = []
        self.ftp.retrlines("LIST " + caminho, linhas.append)
        out = []
        desconhecidas = 0
        for linha in linhas:
            e = parse_linha_lista(linha)
            if e is None:
                desconhecidas += 1
            else:
                out.append(e)
        self._calibrar_lista(caminho, out)
        if self._offset_lista:
            for e in out:
                if e.mtime:
                    e.mtime += self._offset_lista
        if desconhecidas and not out:
            # formato que nao conhecemos: pelo menos os nomes, via NLST
            logger.warning("Formato de listagem nao reconhecido; usando NLST "
                           "(sem tamanho nem data).")
            for nome in self.ftp.nlst(caminho):
                nome = nome.rsplit("/", 1)[-1]
                if nome not in (".", ".."):
                    out.append(Entry(name=nome))
        return out

    def _calibrar_lista(self, caminho: str, itens: List[Entry]) -> None:
        """Descobre em que fuso o servidor escreve as datas do LIST.

        O LIST nao tem padrao e nem ano: uns servidores mandam a hora local
        deles, outros mandam GMT, e o cliente nao tem como saber. O MDTM, ao
        contrario, e definido pela RFC como UTC. Entao pergunta-se o MDTM de
        UM arquivo, compara-se com o que o LIST disse e guarda-se a diferenca
        arredondada para o quarto de hora - que e o passo dos fusos que
        existem no mundo. Uma pergunta por sessao resolve a pasta inteira.

        Sem isso, um servidor em GMT faria toda a comparacao por data errar
        pelo tamanho do fuso, e a sincronizacao reenviaria arquivos iguais.
        """
        if self._offset_lista is not None or not self.tem_mdtm:
            return
        amostra = next((e for e in itens if not e.is_dir and e.mtime), None)
        if amostra is None:
            return
        try:
            resposta = self.ftp.sendcmd("MDTM " + self.juntar(caminho,
                                                              amostra.name))
        except ftplib.all_errors:
            self._offset_lista = 0.0
            return
        real = _mtime_mlsd(resposta[4:].strip())
        if not real:
            self._offset_lista = 0.0
            return
        delta = real - amostra.mtime
        if abs(delta) > 26 * 3600:
            # diferenca grande demais para ser fuso: algo mais esta errado,
            # e chutar um offset so pioraria
            self._offset_lista = 0.0
            return
        self._offset_lista = round(delta / 900.0) * 900.0
        if self._offset_lista:
            logger.info("As datas do LIST estao %+.1f h em relacao ao UTC; "
                        "ajustando.", self._offset_lista / 3600.0)

    def stat(self, caminho: str) -> Optional[Entry]:
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return Entry(name="/", is_dir=True)
        # MLST e barato e exato quando existe
        if self.tem_mlsd:
            try:
                resposta = self.ftp.sendcmd("MLST " + caminho)
                for linha in resposta.splitlines()[1:]:
                    linha = linha.strip()
                    if not linha or linha[:3].isdigit():
                        continue
                    fatos, _, nome = linha.partition(" ")
                    d = {}
                    for par in fatos.split(";"):
                        if "=" in par:
                            k, v = par.split("=", 1)
                            d[k.lower()] = v
                    tipo = (d.get("type") or "").lower()
                    eh_dir = tipo in ("dir", "cdir")
                    return Entry(name=self.nome(caminho), is_dir=eh_dir,
                                 size=0 if eh_dir else int(d.get("size") or 0),
                                 mtime=_mtime_mlsd(d.get("modify", "")),
                                 perms=d.get("unix.mode", ""))
            except ftplib.all_errors:
                pass
        return super().stat(caminho)

    def hash_remoto(self, caminho: str, algoritmo: str = "md5"):
        """XMD5/MD5/XCRC, quando o servidor oferecer.

        Sao extensoes nao padronizadas, mas comuns em ProFTPD e servidores de
        hospedagem, e valem muito: com elas da para PROVAR que o arquivo
        baixado e igual ao do servidor, em vez de so comparar tamanho.
        """
        texto = " ".join(self._feat)
        caminho = self.normalizar(caminho)
        tentativas = []
        if algoritmo == "md5":
            if "XMD5" in texto:
                tentativas.append(("XMD5", "md5"))
            if "MD5" in texto:
                tentativas.append(("MD5", "md5"))
        if "XCRC" in texto:
            tentativas.append(("XCRC", "crc32"))
        for comando, nome in tentativas:
            try:
                resposta = self.ftp.sendcmd('%s "%s"' % (comando, caminho))
            except ftplib.all_errors:
                continue
            valor = resposta.split()[-1].strip().strip('"').lower()
            if valor and all(c in "0123456789abcdef" for c in valor):
                return (nome, valor)
        return None

    def tamanho(self, caminho: str) -> int:
        """SIZE do arquivo, -1 se o servidor nao souber responder."""
        try:
            self.ftp.voidcmd("TYPE I")
            n = self.ftp.size(self.normalizar(caminho))
            return int(n) if n is not None else -1
        except Exception:
            return -1

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        try:
            self.ftp.mkd(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def apagar_arquivo(self, caminho: str) -> None:
        try:
            self.ftp.delete(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def apagar_pasta(self, caminho: str) -> None:
        try:
            self.ftp.rmd(self.normalizar(caminho))
        except Exception as e:
            raise _traduzir(e) from e

    def renomear(self, de: str, para: str) -> None:
        try:
            self.ftp.rename(self.normalizar(de), self.normalizar(para))
        except Exception as e:
            raise _traduzir(e) from e

    def chmod(self, caminho: str, modo: int) -> None:
        if not self.pode_chmod:
            raise ErroPermanente("Este servidor nao aceita SITE CHMOD.")
        try:
            self.ftp.sendcmd("SITE CHMOD %03o %s" % (modo,
                                                     self.normalizar(caminho)))
        except Exception as e:
            raise _traduzir(e) from e

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        if not mtime or not self.tem_mfmt:
            return
        carimbo = time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime))
        try:
            self.ftp.sendcmd("MFMT %s %s" % (carimbo, self.normalizar(caminho)))
        except ftplib.all_errors as e:
            # rebaixa a capacidade em vez de estourar de novo a cada arquivo:
            # sem isso, a comparacao por data reenviaria a pasta inteira toda vez
            logger.info("MFMT recusado (%s); a data nao sera preservada.", e)
            self.tem_mfmt = False
            self.preserva_mtime = False

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite=None, cb=None, cancelar=None) -> int:
        caminho = self.normalizar(caminho)
        if offset and not self.resume_download:
            raise ErroPermanente("Este servidor nao aceita retomada (REST).")
        escritos = 0
        restante = limite
        conn = None
        parcial = False
        try:
            self.ftp.voidcmd("TYPE I")
            conn = self.ftp.transfercmd("RETR " + caminho,
                                        rest=offset or None)
            self._prazo(conn, TIMEOUT_PRIMEIRO_BYTE)
            primeiro = True
            while True:
                pedir = BLOCO if restante is None else min(BLOCO, restante)
                if pedir <= 0:
                    parcial = True     # parou por limite, ainda ha dados la
                    break
                dados = conn.recv(pedir)
                if primeiro:
                    # o servidor comecou a responder: solta o prazo curto
                    primeiro = False
                    self._prazo(conn, TIMEOUT_DADOS)
                if not dados:
                    break
                destino_fobj.write(dados)
                escritos += len(dados)
                if restante is not None:
                    restante -= len(dados)
                self._passo(cb, len(dados), cancelar)
        except Cancelado:
            parcial = True
            raise
        except Exception as e:
            self.suja = True
            raise _traduzir(e) from e
        finally:
            self._fechar_dados(conn, parcial)
        return escritos

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb=None, cancelar=None) -> int:
        caminho = self.normalizar(caminho)
        enviados = 0
        conn = None
        comando = "STOR " + caminho
        rest = None
        if offset:
            if self.resume_upload:
                rest = offset
            else:
                # sem REST no envio, APPE e a unica retomada possivel; ela
                # exige que o destino tenha exatamente 'offset' bytes, o que
                # quem chamou ja conferiu
                comando = "APPE " + caminho
        try:
            self.ftp.voidcmd("TYPE I")
            conn = self.ftp.transfercmd(comando, rest=rest)
            self._prazo(conn, TIMEOUT_DADOS)
            while True:
                bloco = origem_fobj.read(BLOCO)
                if not bloco:
                    break
                conn.sendall(bloco)
                enviados += len(bloco)
                self._passo(cb, len(bloco), cancelar)
        except Cancelado:
            self.suja = True
            self._fechar_dados(conn, True)
            raise
        except ftplib.error_perm as e:
            self._fechar_dados(conn, True)
            if rest is not None and str(e)[:3] in ("501", "502", "504"):
                # anunciou REST no FEAT mas recusa no STOR: rebaixa e avisa
                logger.info("O servidor recusou REST no envio; usando APPE.")
                self.resume_upload = False
                return self.enviar(origem_fobj, caminho, tamanho=tamanho,
                                   offset=offset, cb=cb, cancelar=cancelar)
            raise _traduzir(e) from e
        except Exception as e:
            self.suja = True
            self._fechar_dados(conn, True)
            raise _traduzir(e) from e
        self._fechar_dados(conn, False)
        return enviados

    @staticmethod
    def _prazo(conn, segundos: int) -> None:
        try:
            conn.settimeout(segundos)
        except Exception:
            pass

    def _fechar_dados(self, conn, parcial: bool) -> None:
        """Fecha o canal de dados e le a resposta final.

        Quando a transferencia parou no meio (limite de faixa, cancelamento
        ou erro), o servidor ainda tem dados para mandar: ler a resposta ali
        travaria. Nesse caso a conexao e marcada como suja e quem a pegou
        emprestada do pool a descarta em vez de devolver.
        """
        if conn is None:
            return
        try:
            if isinstance(conn, ssl.SSLSocket) and not parcial:
                conn.unwrap()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        if parcial:
            self.suja = True
            return
        try:
            self.ftp.voidresp()
        except Exception as e:
            self.suja = True
            raise _traduzir(e) from e


register(RemoteSpec(
    kind="ftp",
    label="FTP / FTPS",
    factory=lambda site=None: FtpRemote(site),
    porta_padrao=PORTA,
    icone="servidor",
    fields=[
        Field("host", "Servidor", required=True,
              help="Endereco do servidor, sem ftp:// na frente."),
        Field("porta", "Porta", kind="int", default=0, width=8,
              help="0 usa 21 (ou 990 no modo implicito)."),
        Field("usuario", "Usuario", help="Deixe vazio para acesso anonimo."),
        Field("senha", "Senha", kind="password"),
        Field("tls_modo", "Criptografia", kind="escolha", default="explicito",
              opcoes=[("explicito", "FTPS explicito (AUTH TLS) - recomendado"),
                      ("implicito", "FTPS implicito (porta 990)"),
                      ("nenhum", "FTP puro - SEM criptografia")],
              help="FTP puro manda senha e arquivos em texto legivel na rede."),
        Field("passivo", "Modo passivo", kind="bool", default=True,
              help="Desligue so se o servidor exigir modo ativo."),
        Field("encoding", "Codificacao", kind="escolha", default="utf-8",
              opcoes=[("utf-8", "UTF-8"), ("latin-1", "Latin-1 (ISO-8859-1)")],
              help="Servidor antigo que mostra acento errado costuma ser latin-1."),
        Field("pasta_remota", "Pasta inicial",
              help="Onde abrir ao conectar. Vazio usa a pasta do login."),
        Field("max_conexoes", "Conexoes simultaneas", kind="int", default=4,
              width=8, help="Quantas transferencias ao mesmo tempo."),
    ],
    note="FTP puro nao protege nada. Use FTPS sempre que o servidor permitir.",
))
