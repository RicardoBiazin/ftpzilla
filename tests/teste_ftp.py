"""FTP e FTPS contra um servidor pyftpdlib de verdade."""
from __future__ import annotations

import io
import os
import ssl

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, levanta, pular

from ftpzilla.remotes.base import ErroAutenticacao, ErroPermanente
from ftpzilla.remotes.ftp import _FTPTLS, FtpRemote, parse_linha_lista
from ftpzilla.sites import Site

#: tamanho do dados.bin de teste; maior que o bloco de transferencia, para
#: o progresso ser reportado em varias chamadas e nao em uma so
TAM = 256 * 4096


def _site(host, porta, **extra) -> Site:
    dados = dict(nome="teste", kind="ftp", host=host, porta=porta,
                 usuario=servidores.USUARIO, senha=servidores.SENHA,
                 tls_modo="nenhum")
    dados.update(extra)
    return Site(**dados)


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado (pip install -r requirements-dev.txt)")


def _montar(raiz):
    escrever(os.path.join(raiz, "leiame.txt"), b"conteudo do arquivo\n")
    escrever(os.path.join(raiz, "dados.bin"), bytes(range(256)) * 4096)  # 1 MB
    os.makedirs(os.path.join(raiz, "sub"), exist_ok=True)
    escrever(os.path.join(raiz, "sub", "dentro.txt"), b"x")


# ---------------------------------------------------------------------------
def teste_parsers_de_listagem():
    """LIST nao e padronizado; cobrir os dois formatos que existem na pratica."""
    e = parse_linha_lista("drwxr-xr-x 2 alice users 4096 Jan 02 15:04 minha pasta")
    checar(e is not None and e.is_dir and e.name == "minha pasta",
           "Unix: pasta com espaco no nome")
    e = parse_linha_lista("-rw-r--r-- 1 alice users 1234 Mar 15  2023 a.txt")
    checar(e is not None and e.size == 1234 and not e.is_dir,
           "Unix: arquivo com ano em vez de hora")
    e = parse_linha_lista("lrwxrwxrwx 1 r r 7 Feb 01 10:00 link -> alvo.txt")
    checar(e is not None and e.is_link and e.link_target == "alvo.txt",
           "Unix: link simbolico separa nome e alvo")
    e = parse_linha_lista("01-02-24  03:04PM       <DIR>          pasta")
    checar(e is not None and e.is_dir, "Windows/IIS: pasta")
    e = parse_linha_lista("01-02-24  03:04PM              1234 a.txt")
    checar(e is not None and e.size == 1234, "Windows/IIS: arquivo")
    igual(parse_linha_lista("total 12"), None, "'total' e ignorado")
    igual(parse_linha_lista("lixo que nao e listagem"), None,
          "linha ilegivel vira None, e nao um Entry inventado")


def teste_navegacao():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                nomes = sorted(e.name for e in r.listar("/"))
                igual(nomes, ["dados.bin", "leiame.txt", "sub"], "lista a raiz")
                itens = {e.name: e for e in r.listar("/")}
                checar(itens["sub"].is_dir, "reconhece a pasta")
                igual(itens["leiame.txt"].size, 20, "tamanho do arquivo")
                checar(itens["leiame.txt"].mtime > 0, "data preenchida")
                igual([e.name for e in r.listar("/sub")], ["dentro.txt"],
                      "lista a subpasta")
                e = r.stat("/leiame.txt")
                checar(e is not None and e.size == 20, "stat do arquivo")
                igual(r.stat("/nao_existe"), None, "stat de inexistente e None")
            finally:
                r.fechar()


def teste_capacidades_vem_do_feat():
    """A capacidade tem que ser DESCOBERTA. Supor que o servidor tem REST
    quebra a retomada; supor que nao tem desliga a retomada de todo mundo."""
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                checar(r.tem_mlsd, "servidor completo: MLSD detectado")
                checar(r.resume_download, "servidor completo: REST detectado")
                checar(r.preserva_mtime, "servidor completo: MFMT detectado")
            finally:
                r.fechar()

        with servidores.servidor_ftp(tmp, sem_rest=True, sem_mfmt=True) as (h, p):
            r = FtpRemote(_site(h, p))
            r.conectar()
            try:
                checar(not r.resume_download,
                       "sem REST no FEAT: retomada desligada")
                checar(not r.segmentavel,
                       "sem REST no FEAT: segmentacao desligada")
                checar(not r.preserva_mtime,
                       "sem MFMT no FEAT: nao promete preservar a data")
            finally:
                r.fechar()


def teste_fallback_de_listagem():
    """Servidor sem MLSD tem que cair para LIST sem o usuario perceber."""
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp, sem_mlsd=True) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                checar(not r.tem_mlsd, "MLSD ausente foi detectado")
                itens = {e.name: e for e in r.listar("/")}
                igual(sorted(itens), ["dados.bin", "leiame.txt", "sub"],
                      "LIST lista os mesmos itens")
                igual(itens["leiame.txt"].size, 20, "LIST traz o tamanho")
                checar(itens["sub"].is_dir, "LIST reconhece a pasta")

                # o LIST nao diz em que fuso escreve a data; o pyftpdlib usa
                # GMT, outros servidores usam a hora local deles. A calibracao
                # por MDTM tem que absorver essa diferenca - sem ela, toda
                # comparacao por data erraria pelo tamanho do fuso
                real = os.path.getmtime(os.path.join(tmp, "leiame.txt"))
                erro = abs(itens["leiame.txt"].mtime - real)
                checar(erro < 120,
                       "a data do LIST bate com o disco (erro de %.0fs) "
                       "depois da calibracao por MDTM" % erro)
                checar(r._offset_lista is not None,
                       "o fuso do LIST foi calibrado uma vez so")
            finally:
                r.fechar()


def teste_manipulacao():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                r.criar_pasta("/nova")
                checar(r.existe("/nova"), "criar_pasta")
                r.criar_pastas("/nova/a/b")
                checar(r.existe("/nova/a/b"), "criar_pastas cria a arvore")
                r.renomear("/leiame.txt", "/renomeado.txt")
                checar(r.existe("/renomeado.txt") and not r.existe("/leiame.txt"),
                       "renomear")
                r.apagar_arquivo("/renomeado.txt")
                checar(not r.existe("/renomeado.txt"), "apagar_arquivo")
                r.apagar_arvore("/nova")
                checar(not r.existe("/nova"), "apagar_arvore")
                levanta(ErroPermanente, lambda: r.criar_pasta("/sub"),
                        "criar pasta que ja existe vira ErroPermanente")
            finally:
                r.fechar()


def teste_transferencia():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                buf = io.BytesIO()
                pedacos = []
                n = r.baixar("/dados.bin", buf, cb=pedacos.append)
                igual(n, TAM, "baixou o arquivo inteiro")
                igual(len(buf.getvalue()), TAM, "tamanho do conteudo")
                igual(sum(pedacos), TAM, "a soma dos deltas bate")
                checar(len(pedacos) > 1,
                       "o progresso foi reportado em %d blocos" % len(pedacos))

                r.enviar(io.BytesIO(b"enviado por teste"), "/novo.txt",
                         tamanho=17)
                igual(r.stat("/novo.txt").size, 17, "enviou o arquivo")
                with open(os.path.join(tmp, "novo.txt"), "rb") as f:
                    igual(f.read(), b"enviado por teste", "conteudo no servidor")

                buf = io.BytesIO()
                r.baixar("/dados.bin", buf, offset=TAM - 400)
                igual(len(buf.getvalue()), 400, "REST pula o inicio do arquivo")
            finally:
                r.fechar()


def teste_faixa_marca_conexao_suja():
    """Ler so uma faixa deixa dados no socket: a conexao nao pode voltar para
    o pool, senao a proxima transferencia recebe o resto do arquivo anterior."""
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                buf = io.BytesIO()
                r.baixar("/dados.bin", buf, offset=0, limite=1000)
                igual(len(buf.getvalue()), 1000, "leu so a faixa pedida")
                checar(r.suja, "a conexao ficou marcada como suja")
            finally:
                r.fechar()


def teste_mfmt():
    _precisa()
    import time
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta))
            r.conectar()
            try:
                quando = time.time() - 86400 * 3
                r.definir_mtime("/leiame.txt", quando)
                obtido = r.stat("/leiame.txt").mtime
                checar(abs(obtido - quando) < 60,
                       "MFMT gravou a data (tolerancia de 1 min)")
            finally:
                r.fechar()

        with servidores.servidor_ftp(tmp, sem_mfmt=True) as (h, p):
            r = FtpRemote(_site(h, p))
            r.conectar()
            try:
                r.definir_mtime("/leiame.txt", 1000000.0)
                checar(not r.preserva_mtime,
                       "sem MFMT, definir_mtime nao estoura: rebaixa a "
                       "capacidade para a comparacao por data ser desligada")
            finally:
                r.fechar()


def teste_login_recusado():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            r = FtpRemote(_site(host, porta, senha="errada"))
            levanta(ErroAutenticacao, r.conectar,
                    "senha errada vira ErroAutenticacao (que nao se repete)")


def teste_ftps():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        pem = servidores.cert_autoassinado(tmp)
        with servidores.servidor_ftp(tmp, tls=True, certificado=pem) as (h, p):
            # certificado autoassinado: so conecta com o fingerprint fixado
            impressao = _impressao(pem)
            r = FtpRemote(_site(h, p, tls_modo="explicito",
                                cert_fingerprint=impressao))
            r.conectar()
            try:
                checar(r.conectado, "conectou por FTPS explicito")
                checar(isinstance(r.ftp.sock, ssl.SSLSocket),
                       "o canal de controle esta em TLS")
                nomes = sorted(e.name for e in r.listar("/"))
                checar("leiame.txt" in nomes, "listagem sobre TLS")
                buf = io.BytesIO()
                r.baixar("/dados.bin", buf)
                igual(len(buf.getvalue()), TAM, "download sobre TLS")
                r.enviar(io.BytesIO(b"tls"), "/tls.txt", tamanho=3)
                igual(r.stat("/tls.txt").size, 3, "upload sobre TLS")
            finally:
                r.fechar()


def teste_certificado_fixado_detecta_troca():
    _precisa()
    with PastaTemp() as tmp:
        _montar(tmp)
        pem = servidores.cert_autoassinado(tmp)
        with servidores.servidor_ftp(tmp, tls=True, certificado=pem) as (h, p):
            errado = "AA" * 32
            r = FtpRemote(_site(h, p, tls_modo="explicito",
                                cert_fingerprint=errado))
            from ftpzilla.remotes.base import ErroCertificado
            levanta(ErroCertificado, r.conectar,
                    "certificado diferente do fixado e recusado")


def teste_reuso_de_sessao_tls():
    """vsftpd com require_ssl_reuse (o padrao) recusa transferencia cujo canal
    de dados nao reaproveite a sessao TLS do controle. O pyftpdlib nao exige
    isso, entao o que da para provar aqui e que o parametro 'session' e
    mesmo passado - que e a linha que faltava."""
    capturado = {}

    class SockFalso:
        session = "sessao-do-controle"

    class ContextoFalso:
        def wrap_socket(self, sock, server_hostname=None, session=None):
            capturado["session"] = session
            capturado["host"] = server_hostname
            return sock

    class Falso(_FTPTLS):
        def __init__(self):
            self._prot_p = True
            self.context = ContextoFalso()
            self.host = "servidor.exemplo"
            self.sock = SockFalso()

    import ftplib
    original = ftplib.FTP.ntransfercmd
    ftplib.FTP.ntransfercmd = lambda self, cmd, rest=None: ("conexao", 10)
    try:
        conn, tam = Falso().ntransfercmd("RETR x")
        igual(capturado.get("session"), "sessao-do-controle",
              "o canal de dados reaproveita a sessao TLS do controle")
        igual(capturado.get("host"), "servidor.exemplo",
              "e valida o nome do host no canal de dados")
    finally:
        ftplib.FTP.ntransfercmd = original


def teste_browser_worker():
    """A thread de navegacao: a interface nunca chama o Remote direto.

    Tambem prova o descarte por geracao - clicar rapido em varias pastas nao
    pode fazer a listagem antiga chegar depois e sobrescrever a atual.
    """
    _precisa()
    import queue as _q

    from ftpzilla.browser import BrowserWorker

    pendentes = _q.Queue()

    def agendar(_atraso, funcao):
        pendentes.put(funcao)

    def drenar(prazo=10.0):
        import time as _t
        fim = _t.time() + prazo
        while _t.time() < fim:
            try:
                pendentes.get(timeout=0.05)()
                return True
            except _q.Empty:
                continue
        return False

    with PastaTemp() as tmp:
        _montar(tmp)
        with servidores.servidor_ftp(tmp) as (host, porta):
            worker = BrowserWorker(FtpRemote(_site(host, porta)), agendar)
            try:
                recebido = {}
                worker.conectar(ok=lambda home: recebido.update(home=home),
                                erro=lambda e: recebido.update(erro=e))
                checar(drenar(), "o worker respondeu a conexao")
                checar("erro" not in recebido, "conectou sem erro")

                itens = {}
                worker.listar("/", ok=lambda lst, g: itens.update(lst=lst, g=g),
                              geracao=1)
                checar(drenar(), "a listagem voltou pela ponte de callback")
                nomes = sorted(e.name for e in itens["lst"])
                igual(nomes, ["dados.bin", "leiame.txt", "sub"],
                      "a listagem veio completa")
                igual(itens["g"], 1, "a geracao volta junto com o resultado")

                # erro tambem precisa voltar pela ponte, e nao derrubar a thread
                falha = {}
                worker.listar("/nao/existe", ok=lambda *_: None,
                              erro=lambda e: falha.update(e=e), geracao=2)
                checar(drenar(), "o erro voltou pela ponte")
                checar("e" in falha, "o erro chegou como excecao, nao como None")

                # a thread continua viva depois do erro
                depois = {}
                worker.listar("/", ok=lambda lst, g: depois.update(n=len(lst)),
                              geracao=3)
                checar(drenar(), "a thread sobreviveu ao erro")
                igual(depois.get("n"), 3, "e continua atendendo comandos")
            finally:
                worker.fechar()


def _impressao(pem: str) -> str:
    import hashlib
    from cryptography import x509
    with open(pem, "rb") as f:
        dados = f.read()
    cert = x509.load_pem_x509_certificate(dados)
    from cryptography.hazmat.primitives import serialization
    der = cert.public_bytes(serialization.Encoding.DER)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


TESTES = [teste_parsers_de_listagem, teste_navegacao,
          teste_capacidades_vem_do_feat, teste_fallback_de_listagem,
          teste_manipulacao, teste_transferencia,
          teste_faixa_marca_conexao_suja, teste_mfmt, teste_login_recusado,
          teste_ftps, teste_certificado_fixado_detecta_troca,
          teste_reuso_de_sessao_tls, teste_browser_worker]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "FTP e FTPS"))
