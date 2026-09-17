"""Servidores HTTP que imitam o Microsoft Graph e o Google Drive.

Testar nuvem contra a nuvem de verdade seria lento, precisaria de conta e
falharia sozinho quando a rede caisse. Aqui os dois servicos sao imitados no
que importa para o cliente: listagem paginada, download com Range, envio em
pedacos com Content-Range, e o 401 que obriga a renovar o token.

O 401 e o detalhe mais util: o token de acesso tem vida curta e e revogavel,
entao o caminho "recebeu 401, renovou, repetiu" precisa funcionar - e e
justamente o que so aparece em producao, horas depois, se estiver quebrado.
"""
from __future__ import annotations

import calendar
import contextlib
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_VALIDO = "token-bom"
TOKEN_VELHO = "token-vencido"
REFRESH = "refresh-de-teste"


class _No:
    """Um item da arvore imaginaria."""

    def __init__(self, nome, pasta=False, dados=b"", pai=None, ident=""):
        self.nome = nome
        self.pasta = pasta
        self.dados = dados
        self.pai = pai
        self.id = ident
        self.mtime = time.time()


class Estado:
    """A arvore compartilhada pelos dois servidores falsos."""

    def __init__(self):
        self.nos = {}
        self.proximo = 1
        self.sessoes = {}          # url de envio -> {'no', 'buffer'}
        self.tokens_renovados = 0
        self.exigir_token_novo = False
        self.raiz = self.criar("", pasta=True, pai=None, ident="root")

    def criar(self, nome, pasta=False, dados=b"", pai=None, ident=""):
        if not ident:
            ident = "id%d" % self.proximo
            self.proximo += 1
        no = _No(nome, pasta, dados, pai, ident)
        self.nos[ident] = no
        return no

    # --- caminhos ---------------------------------------------------------
    def filhos(self, pai_id):
        return [n for n in self.nos.values() if n.pai == pai_id]

    def por_caminho(self, caminho):
        caminho = (caminho or "/").strip("/")
        atual = self.raiz
        if not caminho:
            return atual
        for parte in caminho.split("/"):
            achou = None
            for n in self.filhos(atual.id):
                if n.nome == parte:
                    achou = n
                    break
            if achou is None:
                return None
            atual = achou
        return atual

    def caminho_de(self, no):
        partes = []
        while no is not None and no.id != "root":
            partes.append(no.nome)
            no = self.nos.get(no.pai)
        return "/" + "/".join(reversed(partes))

    def apagar(self, no):
        for filho in list(self.filhos(no.id)):
            self.apagar(filho)
        self.nos.pop(no.id, None)

    def povoar(self, arquivos):
        """arquivos: {'/a.txt': b'...', '/pasta/b.txt': b'...'}"""
        for caminho, dados in arquivos.items():
            partes = caminho.strip("/").split("/")
            atual = self.raiz
            for parte in partes[:-1]:
                achou = next((n for n in self.filhos(atual.id)
                              if n.nome == parte), None)
                if achou is None:
                    achou = self.criar(parte, pasta=True, pai=atual.id)
                atual = achou
            self.criar(partes[-1], dados=dados, pai=atual.id)


class _Base(BaseHTTPRequestHandler):
    estado: Estado = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # --- utilidades -------------------------------------------------------
    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, codigo, dados, extras=None):
        corpo = json.dumps(dados).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in (extras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _bytes(self, codigo, dados, extras=None):
        self.send_response(codigo)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(dados)))
        for k, v in (extras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(dados)

    def _vazio(self, codigo=204):
        self.send_response(codigo)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _autorizado(self) -> bool:
        cab = self.headers.get("Authorization", "")
        return cab == "Bearer " + TOKEN_VALIDO

    def _faixa(self, total):
        faixa = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d*)", faixa or "")
        if not m:
            return None
        ini = int(m.group(1))
        fim = int(m.group(2)) if m.group(2) else total - 1
        return ini, min(fim, total - 1)

    def _token(self, caminho):
        """Endpoint de token, comum aos dois servicos."""
        self._corpo()
        self.estado.tokens_renovados += 1
        self._json(200, {"access_token": TOKEN_VALIDO, "expires_in": 3600,
                         "refresh_token": REFRESH})


@contextlib.contextmanager
def _subir(classe, estado):
    classe.estado = estado
    # ThreadingHTTPServer, e nao HTTPServer: com HTTP/1.1 o cliente mantem a
    # conexao viva, e um servidor de uma thread so ficaria preso nela sem
    # conseguir aceitar a segunda conexao do pool - o teste travaria sem dizer
    # por que
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), classe)
    servidor.daemon_threads = True
    t = threading.Thread(target=servidor.serve_forever, daemon=True)
    t.start()
    host, porta = servidor.server_address[:2]
    try:
        yield "http://%s:%d" % (host, porta)
    finally:
        servidor.shutdown()
        servidor.server_close()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# Microsoft Graph
# ---------------------------------------------------------------------------
class _Graph(_Base):

    def _item(self, no):
        d = {"id": no.id, "name": no.nome,
             "lastModifiedDateTime": time.strftime(
                 "%Y-%m-%dT%H:%M:%SZ", time.gmtime(no.mtime)),
             "eTag": '"%s"' % no.id}
        if no.pasta:
            d["folder"] = {"childCount": len(self.estado.filhos(no.id))}
        else:
            d["file"] = {}
            d["size"] = len(no.dados)
        return d

    def _alvo(self, caminho_url):
        """Traduz /me/drive/root:/a/b: em um no da arvore."""
        if caminho_url.startswith("/me/drive/root:"):
            resto = caminho_url[len("/me/drive/root:"):]
            caminho, _, sufixo = resto.partition(":")
            return (urllib.parse.unquote(caminho), sufixo.strip("/"))
        if caminho_url.startswith("/me/drive/root"):
            return ("/", caminho_url[len("/me/drive/root"):].strip("/"))
        return (None, "")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/token":
            return self._token(url.path)
        if url.path.startswith("/sessao/"):
            sessao = self.estado.sessoes.get(url.path)
            if sessao is None:
                return self._json(404, {"error": "sessao desconhecida"})
            return self._json(200, {"nextExpectedRanges":
                                    ["%d-" % len(sessao["buffer"])]})
        if not self._autorizado():
            return self._json(401, {"error": {"code": "InvalidAuthentication"}})

        caminho, sufixo = self._alvo(url.path)
        if caminho is None:
            return self._json(404, {"error": "rota desconhecida"})
        no = self.estado.por_caminho(caminho)
        if no is None:
            return self._json(404, {"error": {"code": "itemNotFound"}})

        if sufixo == "children":
            filhos = [self._item(n) for n in self.estado.filhos(no.id)]
            params = urllib.parse.parse_qs(url.query)
            # paginacao de proposito em duas paginas, para exercitar o
            # nextLink - o erro classico e repetir os params e tomar 400
            if len(filhos) > 2 and "pagina2" not in params:
                base = "%s://%s" % ("http", self.headers.get("Host"))
                return self._json(200, {
                    "value": filhos[:2],
                    "@odata.nextLink": "%s%s?pagina2=1" % (base, url.path)})
            if "pagina2" in params:
                return self._json(200, {"value": filhos[2:]})
            return self._json(200, {"value": filhos})

        if sufixo == "content":
            faixa = self._faixa(len(no.dados))
            if faixa:
                ini, fim = faixa
                return self._bytes(206, no.dados[ini:fim + 1],
                                   {"Content-Range": "bytes %d-%d/%d"
                                    % (ini, fim, len(no.dados))})
            return self._bytes(200, no.dados)

        return self._json(200, self._item(no))

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/token":
            return self._token(url.path)
        if not self._autorizado():
            self._corpo()
            return self._json(401, {"error": {"code": "InvalidAuthentication"}})
        corpo = self._corpo()
        caminho, sufixo = self._alvo(url.path)
        if sufixo == "children":
            pai = self.estado.por_caminho(caminho)
            dados = json.loads(corpo or b"{}")
            nome = dados.get("name", "")
            if any(n.nome == nome for n in self.estado.filhos(pai.id)):
                return self._json(409, {"error": {"code": "nameAlreadyExists"}})
            no = self.estado.criar(nome, pasta="folder" in dados, pai=pai.id)
            return self._json(201, self._item(no))
        if sufixo == "createUploadSession":
            chave = "/sessao/%d" % (len(self.estado.sessoes) + 1)
            self.estado.sessoes[chave] = {"caminho": caminho, "buffer": b""}
            base = "http://%s" % self.headers.get("Host")
            return self._json(200, {"uploadUrl": base + chave})
        return self._json(404, {"error": "rota desconhecida"})

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        corpo = self._corpo()
        if url.path.startswith("/sessao/"):
            sessao = self.estado.sessoes.get(url.path)
            if sessao is None:
                return self._json(404, {"error": "sessao desconhecida"})
            faixa = self.headers.get("Content-Range", "")
            m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", faixa)
            if not m:
                return self._json(400, {"error": "Content-Range ausente"})
            ini, fim, total = int(m.group(1)), int(m.group(2)), m.group(3)
            if ini != len(sessao["buffer"]):
                return self._json(416, {"error": "pedaco fora de ordem"})
            sessao["buffer"] += corpo
            if total != "*" and len(sessao["buffer"]) >= int(total):
                self._gravar(sessao["caminho"], sessao["buffer"])
                self.estado.sessoes.pop(url.path, None)
                return self._json(201, {"id": "novo"})
            return self._json(202, {"nextExpectedRanges":
                                    ["%d-" % len(sessao["buffer"])]})

        if not self._autorizado():
            return self._json(401, {"error": {"code": "InvalidAuthentication"}})
        caminho, sufixo = self._alvo(url.path)
        if sufixo == "content":
            self._gravar(caminho, corpo)
            return self._json(201, {"id": "novo"})
        return self._json(404, {"error": "rota desconhecida"})

    def do_PATCH(self):
        url = urllib.parse.urlparse(self.path)
        corpo = self._corpo()
        if not self._autorizado():
            return self._json(401, {"error": {"code": "InvalidAuthentication"}})
        caminho, _ = self._alvo(url.path)
        no = self.estado.por_caminho(caminho)
        if no is None:
            return self._json(404, {"error": {"code": "itemNotFound"}})
        dados = json.loads(corpo or b"{}")
        if "name" in dados:
            no.nome = dados["name"]
        info = dados.get("fileSystemInfo") or {}
        if info.get("lastModifiedDateTime"):
            # timegm, e nao mktime: os dois servicos falam UTC, e mktime
            # interpretaria como hora local - a data chegaria deslocada pelo
            # tamanho do fuso de quem esta rodando o teste
            no.mtime = calendar.timegm(time.strptime(
                info["lastModifiedDateTime"][:19], "%Y-%m-%dT%H:%M:%S"))
        return self._json(200, self._item(no))

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        if not self._autorizado():
            return self._json(401, {"error": {"code": "InvalidAuthentication"}})
        caminho, _ = self._alvo(url.path)
        no = self.estado.por_caminho(caminho)
        if no is None:
            return self._json(404, {"error": {"code": "itemNotFound"}})
        self.estado.apagar(no)
        return self._vazio(204)

    def _gravar(self, caminho, dados):
        partes = caminho.strip("/").split("/")
        pai = self.estado.por_caminho("/" + "/".join(partes[:-1]))
        existente = next((n for n in self.estado.filhos(pai.id)
                          if n.nome == partes[-1]), None)
        if existente is not None:
            existente.dados = dados
            existente.mtime = time.time()
        else:
            self.estado.criar(partes[-1], dados=dados, pai=pai.id)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
class _Drive(_Base):

    def _item(self, no):
        import hashlib
        d = {"id": no.id, "name": no.nome,
             "modifiedTime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(no.mtime))}
        if no.pasta:
            d["mimeType"] = "application/vnd.google-apps.folder"
        else:
            d["mimeType"] = "application/octet-stream"
            d["size"] = str(len(no.dados))
            d["md5Checksum"] = hashlib.md5(no.dados).hexdigest()
        return d

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/token":
            return self._token(url.path)
        if not self._autorizado():
            return self._json(401, {"error": {"code": 401}})
        params = urllib.parse.parse_qs(url.query)

        if url.path == "/drive/v3/files":
            consulta = (params.get("q") or [""])[0]
            m = re.search(r"'([^']+)' in parents", consulta)
            pai = m.group(1) if m else "root"
            nome = None
            m2 = re.search(r"name = '([^']*)'", consulta)
            if m2:
                nome = m2.group(1).replace("\\'", "'").replace("\\\\", "\\")
            filhos = [n for n in self.estado.filhos(pai)
                      if nome is None or n.nome == nome]
            # paginacao em duas paginas para exercitar o pageToken
            if nome is None and len(filhos) > 2 and "pageToken" not in params:
                return self._json(200, {
                    "files": [self._item(n) for n in filhos[:2]],
                    "nextPageToken": "pag2"})
            if "pageToken" in params:
                filhos = filhos[2:]
            return self._json(200, {"files": [self._item(n) for n in filhos]})

        m = re.match(r"/drive/v3/files/([^/]+)$", url.path)
        if m:
            no = self.estado.nos.get(m.group(1))
            if no is None:
                return self._json(404, {"error": {"code": 404}})
            if (params.get("alt") or [""])[0] == "media":
                faixa = self._faixa(len(no.dados))
                if faixa:
                    ini, fim = faixa
                    return self._bytes(206, no.dados[ini:fim + 1],
                                       {"Content-Range": "bytes %d-%d/%d"
                                        % (ini, fim, len(no.dados))})
                return self._bytes(200, no.dados)
            return self._json(200, self._item(no))
        return self._json(404, {"error": "rota desconhecida"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/token":
            return self._token(url.path)
        corpo = self._corpo()
        if not self._autorizado():
            return self._json(401, {"error": {"code": 401}})
        dados = json.loads(corpo or b"{}")

        if url.path == "/upload/drive/v3/files":
            pais = dados.get("parents") or ["root"]
            chave = "/sessao/%d" % (len(self.estado.sessoes) + 1)
            self.estado.sessoes[chave] = {"nome": dados.get("name", ""),
                                          "pai": pais[0], "buffer": b"",
                                          "id": ""}
            base = "http://%s" % self.headers.get("Host")
            return self._json(200, {}, {"Location": base + chave})

        if url.path == "/drive/v3/files":
            pais = dados.get("parents") or ["root"]
            pasta = dados.get("mimeType") == \
                "application/vnd.google-apps.folder"
            no = self.estado.criar(dados.get("name", ""), pasta=pasta,
                                   pai=pais[0])
            return self._json(200, self._item(no))
        return self._json(404, {"error": "rota desconhecida"})

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        corpo = self._corpo()
        if url.path.startswith("/sessao/"):
            sessao = self.estado.sessoes.get(url.path)
            if sessao is None:
                return self._json(404, {"error": "sessao desconhecida"})
            faixa = self.headers.get("Content-Range", "")
            m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", faixa)
            if not m:
                return self._json(400, {"error": "Content-Range ausente"})
            ini, fim, total = int(m.group(1)), int(m.group(2)), m.group(3)
            if ini != len(sessao["buffer"]):
                return self._json(416, {"error": "pedaco fora de ordem"})
            sessao["buffer"] += corpo
            if total != "*" and len(sessao["buffer"]) >= int(total):
                alvo = sessao.get("id")
                if alvo and alvo in self.estado.nos:
                    no = self.estado.nos[alvo]
                    no.dados = sessao["buffer"]
                else:
                    existente = next(
                        (n for n in self.estado.filhos(sessao["pai"])
                         if n.nome == sessao["nome"]), None)
                    if existente is not None:
                        existente.dados = sessao["buffer"]
                        no = existente
                    else:
                        no = self.estado.criar(sessao["nome"],
                                               dados=sessao["buffer"],
                                               pai=sessao["pai"])
                self.estado.sessoes.pop(url.path, None)
                return self._json(200, self._item(no))
            return self._json(308, {})
        return self._json(404, {"error": "rota desconhecida"})

    def do_PATCH(self):
        url = urllib.parse.urlparse(self.path)
        corpo = self._corpo()
        if not self._autorizado():
            return self._json(401, {"error": {"code": 401}})
        dados = json.loads(corpo or b"{}")

        m = re.match(r"/upload/drive/v3/files/([^/]+)$", url.path)
        if m:
            chave = "/sessao/%d" % (len(self.estado.sessoes) + 1)
            no = self.estado.nos.get(m.group(1))
            self.estado.sessoes[chave] = {"nome": dados.get("name", ""),
                                          "pai": no.pai if no else "root",
                                          "buffer": b"", "id": m.group(1)}
            base = "http://%s" % self.headers.get("Host")
            return self._json(200, {}, {"Location": base + chave})

        m = re.match(r"/drive/v3/files/([^/]+)$", url.path)
        if m:
            no = self.estado.nos.get(m.group(1))
            if no is None:
                return self._json(404, {"error": {"code": 404}})
            if "name" in dados:
                no.nome = dados["name"]
            if "modifiedTime" in dados:
                no.mtime = calendar.timegm(time.strptime(
                    dados["modifiedTime"][:19], "%Y-%m-%dT%H:%M:%S"))
            params = urllib.parse.parse_qs(url.query)
            if "addParents" in params:
                no.pai = params["addParents"][0]
            return self._json(200, self._item(no))
        return self._json(404, {"error": "rota desconhecida"})

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        if not self._autorizado():
            return self._json(401, {"error": {"code": 401}})
        m = re.match(r"/drive/v3/files/([^/]+)$", url.path)
        if m:
            no = self.estado.nos.get(m.group(1))
            if no is None:
                return self._json(404, {"error": {"code": 404}})
            self.estado.apagar(no)
            return self._vazio(204)
        return self._json(404, {"error": "rota desconhecida"})


# ---------------------------------------------------------------------------
@contextlib.contextmanager
def servidor_graph(estado: Estado):
    with _subir(_Graph, estado) as base:
        yield base


@contextlib.contextmanager
def servidor_drive(estado: Estado):
    with _subir(_Drive, estado) as base:
        yield base
