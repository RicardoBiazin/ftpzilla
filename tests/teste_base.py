"""Contratos da abstracao Remote e do registro de tipos."""
from __future__ import annotations

import ajuda
from ajuda import checar, igual, levanta

from ftpzilla import remotes, util
from ftpzilla.browser import entradas_ordenadas
from ftpzilla.remotes.base import Entry, ErroPermanente, Remote


def teste_registro():
    checar("local" in remotes.kinds(), "o tipo 'local' esta registrado")
    spec = remotes.get_spec("local")
    igual(spec.label, "Disco local", "o rotulo do tipo local")
    levanta(ValueError, lambda: remotes.get_spec("inexistente"),
            "tipo desconhecido levanta ValueError")
    igual(remotes.missing_requirements("local"), [],
          "o tipo local nao exige pacote nenhum")


def teste_requisitos_ausentes():
    """missing_requirements devolve o nome de INSTALACAO, nao o do modulo."""
    spec = remotes.RemoteSpec(
        kind="_falso", label="Falso", factory=lambda site=None: None,
        requires=["modulo.que.nao.existe:pacote-no-pip", "os"])
    remotes.register(spec)
    try:
        faltando = remotes.missing_requirements("_falso")
        igual(faltando, ["pacote-no-pip"],
              "devolve o nome do pip e ignora o modulo presente")
        levanta(RuntimeError,
                lambda: remotes.make_remote(type("S", (), {"kind": "_falso"})()),
                "make_remote recusa tipo com dependencia faltando")
    finally:
        remotes._SPECS.pop("_falso", None)


def teste_campos_secretos():
    spec = remotes.RemoteSpec(
        kind="_seg", label="Seg", factory=lambda site=None: None,
        fields=[remotes.Field("usuario", "Usuario"),
                remotes.Field("senha", "Senha", kind="password"),
                remotes.Field("token", "Token", kind="oauth")])
    remotes.register(spec)
    try:
        igual(sorted(remotes.campos_secretos("_seg")), ["senha", "token"],
              "password e oauth sao tratados como segredo")
        checar("usuario" not in remotes.campos_secretos("_seg"),
               "campo de texto comum nao e segredo")
    finally:
        remotes._SPECS.pop("_seg", None)
    igual(remotes.campos_secretos("tipo_que_nao_existe"), ["senha", "passphrase"],
          "tipo desconhecido ainda assim protege o obvio")


def teste_caminhos():
    r = Remote()
    igual(r.normalizar("a/b"), "/a/b", "caminho relativo vira absoluto")
    igual(r.normalizar("/a/b/../c"), "/a/c", "'..' e resolvido")
    igual(r.normalizar("/a\\b"), "/a/b", "barra invertida vira barra")
    igual(r.normalizar(""), "/", "vazio e a raiz")
    igual(r.juntar("/a", "b"), "/a/b", "juntar")
    igual(r.pai("/a/b"), "/a", "pai")
    igual(r.pai("/"), "/", "a raiz e pai de si mesma")
    igual(r.nome("/a/b.txt"), "b.txt", "nome do arquivo")


def teste_capacidades_sao_da_instancia():
    """O FTP so descobre se tem REST depois do FEAT, entao rebaixar a
    capacidade em uma conexao nao pode contaminar as outras."""
    a, b = Remote(), Remote()
    a.preserva_mtime = False
    checar(b.preserva_mtime is True,
           "rebaixar a capacidade de uma instancia nao afeta a outra")
    checar(Remote.preserva_mtime is True, "a classe continua intacta")


def teste_metodos_obrigatorios():
    r = Remote()
    levanta(NotImplementedError, lambda: r.listar("/"),
            "listar e obrigatorio no backend")
    levanta(NotImplementedError, lambda: r.clone(),
            "clone e obrigatorio no backend")
    levanta(ErroPermanente, lambda: r.chmod("/a", 0o644),
            "chmod tem recusa padrao em vez de estourar")


def teste_ordenacao():
    itens = [Entry("b.txt", size=10, mtime=200),
             Entry("Zulu", is_dir=True),
             Entry("a.txt", size=300, mtime=100),
             Entry("alfa", is_dir=True)]

    nomes = [e.name for e in entradas_ordenadas(itens, "nome", False)]
    igual(nomes, ["alfa", "Zulu", "a.txt", "b.txt"],
          "pastas primeiro, nome sem diferenciar maiuscula")

    nomes = [e.name for e in entradas_ordenadas(itens, "nome", True)]
    checar(nomes[:2] == ["Zulu", "alfa"],
           "invertido inverte dentro do grupo...")
    checar(all(not e.is_dir for e in entradas_ordenadas(itens, "nome", True)[2:]),
           "...mas as pastas continuam em cima")

    tam = [e.size for e in entradas_ordenadas(itens, "tamanho", False)
           if not e.is_dir]
    igual(tam, [10, 300], "tamanho ordena como numero, nao como texto")


def teste_formatacao():
    igual(util.fmt_bytes(0), "0 B", "zero byte")
    igual(util.fmt_bytes(1536), "1,5 KB", "virgula decimal")
    igual(util.fmt_bytes(1024 ** 3), "1,0 GB", "gigabyte")
    igual(util.fmt_data(0), "-", "mtime desconhecido nao vira 1970")
    igual(util.fmt_tempo(90), "1m30s", "duracao em minutos")
    igual(util.fmt_tempo(3661), "1h01m", "duracao em horas")
    igual(util.fmt_velocidade(0), "-", "velocidade zero nao mente")
    igual(util.perms_para_texto(0o755), "rwxr-xr-x", "permissao para texto")
    igual(util.perms_para_int("rwxr-xr-x"), 0o755, "permissao de volta")
    igual(util.perms_para_int("644"), 0o644, "permissao em octal")
    checar("..." in util.elidir("a" * 100, 20), "caminho longo e cortado")


TESTES = [teste_registro, teste_requisitos_ausentes, teste_campos_secretos,
          teste_caminhos, teste_capacidades_sao_da_instancia,
          teste_metodos_obrigatorios, teste_ordenacao, teste_formatacao]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Contratos da abstracao"))
