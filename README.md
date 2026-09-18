# FTPZilla

Cliente de transferência de arquivos para Windows — **FTP, FTPS, SFTP, Google Drive e OneDrive** — escrito em Python com interface Tkinter.

Um `.exe` de ~20 MB, sem instalador, sem adware, sem tela de oferta.

## Por que existe

O FileZilla resolve o problema. Estes três incômodos vêm junto:

| No FileZilla | No FTPZilla |
|---|---|
| Senhas guardadas em XML legível na pasta do usuário | Senha, passphrase e token cifrados pela **DPAPI**, amarrados à sua conta do Windows. O arquivo copiado para outra máquina não entrega nada. |
| O instalador oficial vem com patrocinador | Um executável só, baixado do GitHub, gerado por um workflow público. |
| A janela engasga enquanto a fila trabalha | A conexão de navegação é **separada** das de transferência. Navegar nunca espera a fila. |

E mais quatro coisas que o FileZilla livre não faz:

- **Nuvem.** Google Drive e OneDrive entram como qualquer outro servidor — mesma janela, mesma fila, mesma retomada.
- **Retomada validada.** A fila vive em SQLite e sobrevive a fechar o programa ou desligar a máquina. Ao voltar, a transferência continua do byte exato — depois de conferir que o parcial local e o arquivo remoto ainda são os mesmos.
- **Verificação do que chegou.** Quando o servidor sabe calcular hash (`XMD5`/`MD5`/`XCRC` no FTP, MD5 no Drive), o arquivo baixado é comparado com o do servidor. Quando não sabe, o programa diz que conferiu apenas o tamanho, em vez de fingir que verificou.
- **Chave de host SSH conferida de verdade.** Servidor novo pergunta antes de conectar, com a impressão digital no mesmo formato que o OpenSSH imprime. Chave que mudou é recusada com alarde.

## Estado

Pronto e testado — mais de 500 verificações automáticas, quase todas contra servidores de verdade que a própria suíte sobe.

- [x] **M1** — Base do projeto, abstração de acesso a arquivos, disco local, dois painéis navegáveis, temas.
- [x] **M2** — FTP e FTPS navegáveis, gerente de sites, credenciais protegidas.
- [x] **M3** — Fila de transferências persistente.
- [x] **M4** — Retomada e repetição automática.
- [x] **M5** — SFTP.
- [x] **M6** — Download segmentado.
- [x] **M7** — Comparação e sincronização de pastas.
- [x] **M8** — Abas, favoritos, busca remota, editor remoto.
- [x] **M9** — Google Drive e OneDrive.
- [x] **M10** — Executável e empacotamento.

## Como usar

Baixe o `FTPZilla.exe` da [página de versões](https://github.com/RicardoBiazin/ftpzilla/releases) e execute. Não precisa instalar nada.

Para rodar do código (Python 3.11 ou mais novo; testado no 3.13):

```
pip install -r requirements.txt
python app.py
```

| Atalho | O que faz |
|---|---|
| `Ctrl+S` | Gerente de sites (com **Testar conexão** antes de salvar) |
| `Ctrl+T` / `Ctrl+W` | Nova aba local / fechar aba |
| `Ctrl+D` | Comparar a pasta dos dois painéis |
| `Ctrl+F` | Procurar no servidor |
| `F5` / `Backspace` | Atualizar / subir um nível |
| `F2` / `Del` | Renomear / apagar |

Opções da linha de comando:

```
python app.py --tema Claro          # tema inicial
python app.py --pasta-dados .\dados # modo portátil: tudo numa pasta só
python app.py --verboso             # log em nível DEBUG
```

## O que ele faz de diferente, e por quê

### Testar antes de salvar

O gerente de sites tem um botão **Testar conexão** que conecta, lista a pasta inicial e desconecta, sem gravar nada. Além de dizer se conectou, ele relata o que o servidor **aceita**:

```
Conectou em 0,3s. Pasta inicial /, com 41 item(ns).
retomada: sim | data preservada: NÃO | listagem: LIST
```

Isso não é enfeite. "Retomada: não" significa que uma transferência interrompida vai recomeçar do zero; "data preservada: não" significa que a sincronização vai comparar por tamanho, não por data. É melhor saber disso ao cadastrar o site do que no meio de um envio de 2 GB.

### Credenciais

Senha, passphrase de chave e refresh token de OAuth saem do `sites.json` cifrados pela DPAPI do Windows — a chave fica amarrada à sua conta de usuário, e não há chave mestra para guardar em lugar nenhum. Host, usuário e pastas continuam legíveis, para o arquivo seguir servindo de backup e de exportação.

Quem decide o que é segredo não é o gerente de sites: é o próprio backend, no registro de tipos. Assim um protocolo novo protege o campo dele sem ninguém lembrar de mexer no código de gravação — que é exatamente o tipo de esquecimento que vaza credencial.

Quem prefere o Gerenciador de Credenciais do Windows pode usar o `keyring` como backend alternativo. Fora do Windows não há DPAPI: as senhas ficam em texto puro, e o programa **avisa isso na tela**, não só no log.

### Retomada

Retomar sem conferir é como um cliente de FTP corrompe arquivo em silêncio: o resultado fica com o tamanho perfeitamente certo e o conteúdo embaralhado, e ninguém descobre até precisar dele. Por isso o parcial só é reaproveitado quando **tudo** bate — existe no disco, tem exatamente o tamanho que a fila registrou, e o arquivo remoto continua com o mesmo tamanho e a mesma data. Qualquer divergência recomeça do zero e diz por quê.

No envio, destino maior do que o registrado reenvia do zero: se alguém mexeu no arquivo lá, não dá para adivinhar onde continuar.

### Download em várias conexões

Um arquivo grande pode ser baixado em até quatro conexões, cada uma pegando uma faixa. Isso ajuda **em um caso**: servidor que limita a banda *por conexão*, comum em hospedagem compartilhada. Aí a diferença é entre usar 20% e 100% do seu link.

Fora desse caso não ajuda, e pode atrapalhar. Por isso só segmenta quando todas estas condições valem:

- é download (envio segmentado não é confiável em FTP, e em SFTP o ganho não paga o risco);
- o servidor aceita faixa (`REST STREAM` no FTP, `seek` no SFTP);
- o arquivo tem 64 MB ou mais — abrir uma conexão FTPS custa de 0,3 a 1 segundo, o que engole o ganho em arquivo pequeno;
- há conexão sobrando **e ninguém esperando na fila** — senão segmentar só rouba conexão de outro arquivo;
- o destino é um arquivo local comum;
- não é Google Drive nem OneDrive, onde o gargalo é a cota de requisições e mais paralelismo só aproxima o erro 429.

Medindo em rede local ou contra `localhost` você **não vai ver diferença** — não há o que contornar ali.

### Comparar e sincronizar

`Ctrl+D` pinta os dois painéis. O diálogo de sincronização analisa a árvore, mostra linha por linha o que vai acontecer e só executa o que estiver marcado — sincronização apaga arquivo, e nada acontece antes de você ver a lista.

Uma regra do módulo merece destaque: quando algum dos lados **não consegue gravar a data** dos arquivos (FTP sem `MFMT`, por exemplo), a comparação cai automaticamente para tamanho. Sem isso, o servidor carimbaria a data de agora em tudo que recebe, a comparação seguinte acharia tudo diferente, e a sincronização viraria um moinho reenviando a pasta inteira toda vez.

### Editor remoto

Abre o arquivo do servidor no editor do sistema e reenvia quando você salva. O vigia é polling de `os.stat`, e não um observador de sistema de arquivos, por um motivo concreto: Notepad++ e VS Code não gravam por cima do arquivo — eles gravam um temporário e fazem rename, o que para um observador parece o arquivo ter sido *apagado*. O polling só olha o resultado. E só envia depois de dois ciclos com o arquivo estável, senão um arquivo grande subiria no meio da gravação.

## Onde ficam os arquivos

Por padrão em `%APPDATA%\FTPZilla\`:

| Arquivo | O que guarda |
|---|---|
| `sites.json` | Servidores salvos e favoritos. Host, usuário e pastas em texto; **senha e token cifrados**. |
| `fila.db` | Fila de transferências (SQLite). Guarda o `id` do site, **nunca a credencial**. |
| `known_hosts` | Chaves de host SSH aceitas, no formato do OpenSSH. |
| `logs\` | Log diário, com senha e token mascarados antes de gravar. |
| `editor\` | Cópias temporárias dos arquivos abertos no editor remoto. |

`FTPZILLA_HOME` (ou `--pasta-dados`) muda essa pasta — é o que permite levar o programa num pendrive.

## Limites conhecidos

Prefiro dizer do que a pessoa descobrir usando:

- **Arrastar de dentro do FTPZilla para o Explorer não funciona.** Exigiria implementar as interfaces COM `IDataObject`/`IDropSource` e o formato `CFSTR_FILEDESCRIPTOR` para arquivos que ainda nem existem no disco. Em vez de fingir, o menu de contexto tem "Baixar para..." e "Copiar caminho". O caminho contrário — arrastar do Explorer para a janela — funciona com o pacote opcional `tkinterdnd2`.
- **Pastas com dezenas de milhares de arquivos ficam pesadas.** A lista do Tkinter não tem modo virtual. O painel insere em lotes de 500 para não travar, esconde os ícones acima de 5.000 itens e, acima de 20.000, pede que você use o filtro.
- **Nuvem não tem download segmentado**, de propósito (veja acima).
- **Só Windows, de verdade.** Roda em Linux e macOS, mas a proteção de credenciais cai para texto puro.
- **Google Drive e OneDrive exigem um cadastro seu.** Não há credencial embutida no executável: ela seria pública, e os dois provedores não permitem. O cadastro leva um minuto e fica na sua conta; o programa mostra o passo a passo no formulário.

## Gerar o executável

```
pip install -r requirements-dev.txt
build.bat            REM completo
build.bat basico     REM só FTP/FTPS/SFTP, sem os tipos de nuvem
```

Sai em `dist\FTPZilla.exe`. O `.spec` não é versionado: ele é gerado pelo `build.bat` a cada vez, para não ficar desatualizado em relação ao script, que é o que as pessoas leem.

Uma tag `vX.Y.Z` dispara o workflow que roda a bateria inteira e publica o executável.

## Testes

Sem pytest: cada suíte é um script que roda sozinho.

```
python tests\rodar_todos.py      REM tudo
python tests\teste_resume.py     REM uma suíte só
```

Nada depende de servidor externo: a suíte sobe um FTP/FTPS de verdade (`pyftpdlib`), um SFTP de verdade (`paramiko`) e imitações do Microsoft Graph e do Google Drive, tudo dentro do próprio processo. Os testes de retomada **cortam a conexão de propósito** — uma vez, duas vezes, e a cada 80 KB — e conferem o SHA-256 do resultado, porque tamanho certo com conteúdo errado é exatamente o defeito que se quer pegar.

| Suíte | O que cobre |
|---|---|
| `teste_base` | Contratos da abstração e do registro de tipos |
| `teste_local` | Disco local, escrita atômica, cancelamento |
| `teste_seguranca` | DPAPI, mascaramento de log, o que vai para o disco |
| `teste_ftp` | MLSD e LIST, `FEAT`, FTPS, reuso de sessão TLS, fuso do `LIST` |
| `teste_resume` | Conexão cortada, validação de parcial, verificação por hash |
| `teste_fila` | SQLite, prioridade fracionária, sobreviver a fechar o programa |
| `teste_segmentos` | Quando segmentar e, principalmente, quando **não** |
| `teste_sftp` | SFTP, chaves modernas, `known_hosts`, chave trocada |
| `teste_compare` | Estados da comparação e planos de sincronização |
| `teste_busca` | Busca recursiva, editor remoto, favoritos |
| `teste_nuvem` | Drive e OneDrive: paginação, `Range`, envio em pedaços, 401 |
| `teste_gui` | Fumaça da interface, 5.000 linhas, fluxo de ponta a ponta |

## Estrutura

```
app.py                  ponto de entrada
build.bat               gera o executável
ferramentas/            utilidades de desenvolvimento (ícone)
ftpzilla/
  remotes/              acesso a arquivos: base, local, ftp, sftp, gdrive, onedrive
  ui/                   Tkinter: janela, painéis, fila, diálogos, tema, ponte
  browser.py            navegação (uma thread por servidor)
  fila.py fila_store.py transfer.py pool.py medidor.py   o motor
  compare.py busca.py editor.py                          produtividade
  sites.py segredos.py oauth.py                          contas e credenciais
tests/                  suítes executáveis + servidores de teste
```

Uma nota para quem for mexer: **nenhuma thread de trabalho toca em widget**. Tudo volta para a interface por `ui/ponte.py` — uma fila comum de Python drenada por um `after` na thread do Tkinter. Chamar `widget.after()` de outra thread funciona quase sempre, e quando não funciona a operação simplesmente nunca termina, sem erro nenhum no log. Aconteceu aqui, e é o tipo de coisa em que se perde meio dia procurando.

## Como acrescentar um tipo de conexão novo

Só se mexe em um lugar. Crie `ftpzilla/remotes/seu_tipo.py`, herde de `Remote`, implemente `listar`, `stat`, as operações de arquivo e `baixar`/`enviar`, e registre:

```python
register(RemoteSpec(
    kind="seu_tipo",
    label="Seu Tipo",
    factory=lambda site=None: SeuRemote(site),
    requires=["biblioteca:nome-no-pip"],
    fields=[Field("host", "Servidor", required=True),
            Field("senha", "Senha", kind="password")],
))
```

O gerente de sites monta o formulário sozinho, os campos de senha passam a ser cifrados sozinhos, e a dependência ausente vira um aviso amigável em vez de um erro no meio de uma transferência. Declare as capacidades reais (`resume_download`, `segmentavel`, `preserva_mtime`) na instância, depois de conversar com o servidor — a fila e a comparação leem de lá.

## Licença

MIT. Veja [LICENSE](LICENSE).
