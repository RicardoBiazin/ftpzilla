"""Protecao de credenciais, mascaramento de log e o que vai para o disco."""
from __future__ import annotations

import json
import logging
import os
import sys

import ajuda
from ajuda import PastaTemp, checar, igual

from ftpzilla import log as logmod
from ftpzilla import segredos
from ftpzilla.sites import GerenteSites, Site

WINDOWS = sys.platform == "win32"


def teste_dpapi_ida_e_volta():
    if not WINDOWS:
        ajuda.pular("DPAPI so existe no Windows")
    igual(segredos.backend_atual(), "dpapi", "a DPAPI esta disponivel")
    protegido = segredos.proteger("senha muito secreta")
    checar(protegido.startswith("dpapi:"), "o valor sai com o prefixo dpapi:")
    checar("senha muito secreta" not in protegido,
           "o texto original nao aparece no valor protegido")
    igual(segredos.revelar(protegido), "senha muito secreta",
          "revelar devolve o original")
    igual(segredos.proteger(protegido), protegido,
          "proteger duas vezes nao cifra em cima da cifra")
    igual(segredos.revelar("texto solto"), "texto solto",
          "valor sem prefixo volta como esta")
    igual(segredos.proteger(""), "", "vazio continua vazio")


def teste_acentos_e_unicode():
    if not WINDOWS:
        ajuda.pular("DPAPI so existe no Windows")
    for valor in ["senha com acento: ação", "çãéü", "emoji \U0001f512", "a" * 500]:
        igual(segredos.revelar(segredos.proteger(valor)), valor,
              "ida e volta preserva %r" % valor[:24])


def teste_sites_json_nao_guarda_senha_em_claro():
    """O teste que importa: abrir o arquivo gravado e procurar a senha."""
    with PastaTemp() as tmp:
        caminho = os.path.join(tmp, "sites.json")
        g = GerenteSites(caminho)
        g.adicionar(Site(nome="Servidor", kind="ftp", host="exemplo.com",
                         usuario="ricardo", senha="SENHA_EM_CLARO_123"))
        g.salvar()

        bruto = open(caminho, "r", encoding="utf-8").read()
        if WINDOWS:
            checar("SENHA_EM_CLARO_123" not in bruto,
                   "a senha nao aparece no sites.json")
        else:
            ajuda.pular("sem DPAPI: fora do Windows a senha fica em texto")
        checar("exemplo.com" in bruto and "ricardo" in bruto,
               "host e usuario ficam legiveis (o arquivo e exportavel)")

        dados = json.loads(bruto)
        site = dados["sites"][0]
        if WINDOWS:
            checar(site["senha"].startswith("dpapi:"),
                   "o campo senha esta marcado como protegido")

        g2 = GerenteSites(caminho).carregar()
        igual(len(g2), 1, "recarrega o site")
        if WINDOWS:
            igual(g2.sites[0].revelado().senha, "SENHA_EM_CLARO_123",
                  "revelado() devolve a senha para a conexao")
            checar(g2.sites[0].senha != "SENHA_EM_CLARO_123",
                   "o objeto que fica na interface nunca tem a senha em claro")


def teste_campos_secretos_vem_do_registro():
    """Quem decide o que e segredo e o backend, nao o gerente de sites -
    assim um tipo novo protege o proprio campo sem ninguem lembrar disso."""
    from ftpzilla import remotes
    igual(remotes.campos_secretos("ftp"), ["senha"],
          "o tipo ftp declara 'senha' como segredo")
    igual(remotes.campos_secretos("local"), [],
          "o disco local nao tem segredo nenhum")


def teste_mascaramento_do_log():
    casos = [
        ("PASS minhasenha", "minhasenha"),
        ("Enviando senha=abc123 para o servidor", "abc123"),
        ("access_token: ya29.A0ARr-token-longo", "ya29.A0ARr-token-longo"),
        ("Authorization: Bearer abcdef123456", "abcdef123456"),
        ("conectando em ftp://ricardo:senhaurl@servidor.com", "senhaurl"),
        ("client_secret=GOCSPX-abcdef", "GOCSPX-abcdef"),
    ]
    for texto, segredo in casos:
        limpo = logmod.mascarar(texto)
        checar(segredo not in limpo,
               "mascara %s" % texto.split()[0].rstrip(":"))
        checar("***" in limpo, "poe *** no lugar de %r" % segredo[:12])


def teste_filtro_no_logger():
    """O filtro tem que agir no logger, e nao so quando alguem lembra de
    chamar mascarar() - o ftplib loga o PASS sozinho."""
    with PastaTemp():
        logger = logmod.configurar(console=False)
        registros = []

        class Coletor(logging.Handler):
            def emit(self, record):
                registros.append(record.getMessage())

        logger.addHandler(Coletor())
        logger.info("cmd %s", "PASS supersecreta")
        logger.info("senha=%s", "outrasegredo")
        juntos = " ".join(registros)
        checar("supersecreta" not in juntos,
               "senha interpolada por %s tambem e mascarada")
        checar("outrasegredo" not in juntos, "campo senha= mascarado no logger")


def teste_aviso_quando_nao_ha_protecao():
    aviso = segredos.aviso_se_desprotegido()
    if WINDOWS:
        igual(aviso, "", "no Windows nao ha aviso a dar")
    else:
        checar(bool(aviso), "fora do Windows a interface precisa avisar")


def teste_remover_site_esquece_segredo():
    with PastaTemp() as tmp:
        g = GerenteSites(os.path.join(tmp, "sites.json"))
        s = g.adicionar(Site(nome="X", kind="ftp", host="h", senha="p"))
        g.salvar()
        checar(g.remover(s.id), "remover devolve True")
        igual(len(g), 0, "o site saiu da lista")
        checar(not g.remover("id_que_nao_existe"),
               "remover id inexistente devolve False")


TESTES = [teste_dpapi_ida_e_volta, teste_acentos_e_unicode,
          teste_sites_json_nao_guarda_senha_em_claro,
          teste_campos_secretos_vem_do_registro, teste_mascaramento_do_log,
          teste_filtro_no_logger, teste_aviso_quando_nao_ha_protecao,
          teste_remover_site_esquece_segredo]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Seguranca das credenciais"))
