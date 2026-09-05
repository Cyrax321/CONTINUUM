<p align="center">
  <img src="docs/assets/readme-img.png" alt="Banner do CONTINUUM" width="100%" />
</p>

<p align="center">
  <strong>CONTINUUM: Recuperação semântica verificável para agentes de IA de longa duração.</strong>
  Checkpoints semânticos (não dumps de conversa), um ledger idempotente de ações
  que recusa efeitos colaterais duplicados, e um log de eventos encadeado por hash à prova de adulteração,
  tudo exposto como um servidor MCP que nega por padrão. Agnóstico a framework, Python 3.11+.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://pypi.org/project/continuum-agent/"><img src="https://img.shields.io/pypi/v/continuum-agent?style=flat-square&label=PyPI" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square" alt="License" /></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/pydantic-v2-E92063?style=flat-square&logo=pydantic&logoColor=white" alt="Pydantic v2" /></a>
  <a href="https://continuum-nu-six.vercel.app/"><img src="https://img.shields.io/badge/website-live_demo-E06D53?style=flat-square" alt="Website Demo" /></a>
  <a href="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml"><img src="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml/badge.svg" alt="Status CI" /></a>
  <a href="https://app.codecov.io/gh/Cyrax321/CONTINUUM"><img src="https://img.shields.io/codecov/c/github/Cyrax321/CONTINUUM?style=flat-square&logo=codecov" alt="Coverage" /></a>
</p>

<p align="center" style="margin-bottom: 6px;">
  <a href="https://continuum-nu-six.vercel.app/"><strong>Visite o site do CONTINUUM</strong></a>
</p>

<p align="center" style="margin-top: 6px;">
  <a href="https://app.ona.com/#https://github.com/Cyrax321/CONTINUUM"><img src="https://ona.com/build-with-ona.svg" alt="Build with Ona" /></a>
</p>

<p align="center">
  <sub>Se o CONTINUUM ajuda seus agentes a se recuperarem, por favor dê uma estrela ao repositório. Isso ajuda outros a descobri-lo e mantém as boas first issues chegando.</sub>
</p>

<p align="center">
  <sub><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.es.md">Español</a> | <a href="README.ja.md">日本語</a> | <strong>Português</strong> | <a href="README.ko.md">한국어</a></sub>
</p>

---

## Sumário

[Por quê](#por-quê) · [Início rápido](#início-rápido) · [Como funciona](#como-funciona) · [Onde o CONTINUUM se encaixa](#onde-o-continuum-se-encaixa) · [Funcionalidades](#funcionalidades) · [Extensão de segurança](#extensão-de-segurança) · [Verificação empírica](#verificação-empírica) · [Integração MCP](#integração-mcp) · [Integração de frameworks](#integração-de-frameworks) · [Conceitos centrais](#conceitos-centrais) · [Arquitetura](#arquitetura) · [API e CLI](#api-e-cli) · [Roteiro](#roteiro) · [O que o CONTINUUM não é](#o-que-o-continuum-não-é) · [Trabalho relacionado](#trabalho-relacionado) · [Status e limitações](#status-e-limitações) · [Contribuir](#contribuir) · [Licença](#licença)

---

## Por quê

Agentes de IA modernos executam tarefas longas, com centenas de chamadas LLM, invocações de ferramentas e escritas em arquivos e bancos de dados. Quando falham, a resposta usual é reproduzir tudo do zero, o que duplica trabalho, duplica efeitos colaterais, desperdiça tokens e perde decisões.

O CONTINUUM faz uma pergunta mais estreita e mais difícil: um agente pode retomar a partir de uma representação semântica compacta de seu estado de tarefa enquanto verifica de forma independente que esse estado ainda é válido no ambiente atual? Seu diferencial tem três partes:

- **Checkpoints semânticos**: uma representação compacta e versionada do que o agente precisa para continuar, não um despejo de conversa.
- **Revalidação independente do ambiente**: cada componente do checkpoint é verificado contra o ambiente atual antes de retomar, e a obsolescência se propaga pelo grafo de dependências.
- **Estado com proveniência**: cada fato carrega sua origem, de modo que o progresso reportado pelo agente nunca se auto certifica.

## Início rápido

Publicado no PyPI como `continuum-agent` 0.1.0, execute `pip install continuum-agent` (`pip install continuum-agent==0.1.0` para fixar a versão). As tags de release também anexam wheels construídos em [GitHub Releases](https://github.com/Cyrax321/CONTINUUM/releases).

Caminhos sem configuração (sem clonar, sem instalar, sem publicar nada):

| Caminho | Como |
|:--|:--|
| Instalar do PyPI | `pip install continuum-agent==0.1.0` e depois `continuum --help` |
| Ver a recuperação de falha de ponta a ponta | `docker run --rm ghcr.io/cyrax321/continuum` |
| Usar a CLI via Docker | `docker run --rm ghcr.io/cyrax321/continuum continuum --help` |
| Executar a CLI sem clonar | `uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help` |
| Windows PowerShell (de um clone) | `powershell -ExecutionPolicy Bypass -File .\try-it.ps1` ou `powershell -ExecutionPolicy Bypass -File .\try-it.ps1 cli --help` |
| Ambiente de desenvolvimento completo no navegador | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Cyrax321/CONTINUUM?quickstart=1) |

A imagem Docker é publicada no GHCR pelo CI a cada push para `main` e a cada tag de release (`.github/workflows/docker-publish.yml`). O Codespace é definido em `.devcontainer/`.

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

uv venv && source .venv/bin/activate     # macOS / Linux; Windows: .venv\Scripts\activate

# Colaboradores (recomendado): biblioteca + CLI + todas as ferramentas de teste + cada adaptador
uv pip install -e ".[dev]"

# Ou escolha apenas o que precisa: . (mínimo), [mcp], [otel], [langgraph],
# [openai], [langchain], [attest], [postgres]

# Ou pule o clone por completo:
uv pip install git+https://github.com/Cyrax321/CONTINUUM.git
uv pip install "continuum-agent[mcp] @ git+https://github.com/Cyrax321/CONTINUUM.git"
```

> **Alternativa com pip:** substitua `uv pip install` por `pip install` em cada comando acima.

Verifique:

```bash
continuum --help                 # ponto de entrada CLI
continuum-mcp --help             # ponto de entrada do servidor MCP (precisa de [mcp] ou [dev])
pytest -q                        # ~1,380 testes coletados (o número exato e os pulos variam por ambiente)
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum               # os três portões que o CI exige
```

A biblioteca central tem uma única dependência de runtime (`pydantic>=2.7`), todo o resto é opcional. O mapa completo de pacotes, a matriz de extras, a configuração de testes do Postgres e a verificação por comando estão em [references/install.md](references/install.md).

### Conecte um agente de código em dois minutos

Para Claude Code, Gemini CLI ou Codex, você não escreve Python e não precisa de arquivo de prompts:

```bash
continuum start my-task --goal "O que o agente deve fazer"
continuum hooks install claude-code --with-gate   # também: gemini, codex
```

A partir daí cada arquivo que o agente escreve é capturado como evidência encadeada, sua sessão começa com um briefing automático de estado, efeitos colaterais não declarados registrados em `.continuum/gate.json` são recusados antes de disparar, e uma sessão fresca após qualquer queda retoma com próximos passos executáveis. Não é necessário CLAUDE.md.

Exemplo mínimo de biblioteca, registro e recuperação:

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="Analisar 10,000 documentos"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "Analisar 10,000 documentos", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# Após uma queda, um processo novo retoma exatamente de onde parou:
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # já feito, não se repete
print(store.verify_events("run_4821").ok)  # True, cadeia intacta após a queda
```

**Execute a prova você mesmo:**

```bash
python examples/crash_recovery_agent.py   # morte real do processo, efeito colateral real
python examples/context_compaction.py     # transcrição perdida, checkpoint sobrevive
python examples/model_switch.py           # Modelo A morre, Modelo B retoma com segurança
python scripts/mcp_smoke.py               # subprocesso real, tráfego JSON-RPC real
```

O kit `e2e-autonomy-test/` roteiriza uma tarefa real de lotes de faturas, uma morte brusca no meio da execução e uma sessão fresca de retomada, depois pontua o outbox, o ledger e a cadeia de eventos fora de banda. A execução 1 obteve **7/7 em mecânicas** contra uma sessão real do Claude Code. Passo a passo completo em [references/e2e.md](references/e2e.md).

## Como funciona

O CONTINUUM separa o **contexto LLM** (temporário) do **estado duradouro da tarefa** (permanente). Em vez de salvar o histórico de conversa, ele constrói um checkpoint semântico, a mínima informação verificada necessária para continuar.

![Como o CONTINUUM funciona](docs/assets/architecture.svg)

A explicação detalhada, o modelo de projeção e o contexto de recuperação estão em [references/architecture.md](references/architecture.md).

## Onde o CONTINUUM se encaixa

Quatro preocupações se sobrepõem em cada agente de longa duração. O CONTINUUM é dono apenas da última e toca as outras três por meio de costuras explícitas. Nenhum concorrente é nomeado e nenhuma afirmação é feita sem um módulo entregue ou uma suíte publicada que já a imprima.

| Camada | Responde | Como se conecta (módulos entregues ou saídas publicadas) |
|:--|:--|:--|
| Harness | Como o agente chama ferramentas e avança em direção a um objetivo? | Fora do CONTINUUM. Pontos de conexão entregues em `src/continuum/adapters/generic.py` (`GenericAgentAdapter`), `src/continuum/adapters/thin.py` (hooks de CrewAI, AutoGen, Pydantic AI), `src/continuum/mcp/server.py` (MCP stdio), `src/continuum/hooks.py` e `src/continuum/clienthooks.py` (hooks de ciclo de vida de CLI de código), `src/continuum/gateway.py` (proxy HTTP de cumprimento para qualquer linguagem) e `src/continuum/otel.py` (ponte OpenTelemetry). Receitas em `docs/recipes/` e `references/adapters.md`. |
| Execução durável | O que aconteceu antes de uma queda e o que pode ser reproduzido sem perder trabalho? | Log de eventos encadeado `src/continuum/events.py` com `verify()` e `trusted_through`, armazenamento durável `src/continuum/storage/sqlite.py` (WAL, `synchronous=FULL`, schema v6) e `src/continuum/storage/postgres.py` mais `src/continuum/storage/migrations.py`, checkpoints orientados por políticas `src/continuum/checkpoint/manager.py` e `src/continuum/checkpoint/policy.py` que reproduzem a lacuna em `restore()`. Passo a passo em `docs/recovery_walkthrough.md` (saída de `examples/recovery_walkthrough.py`). |
| Plano de controle | Qual execução está ativa, quem pode agir sobre ela e para onde vai a saída? | Registro de execuções e hierarquia pai/filho `src/continuum/storage/` e `src/continuum/recovery/family.py` (`continuum tree`), autorização allowlist `src/continuum/mcp/authz.py` (`CONTINUUM_MCP_MUTATING_CLIENTS` / `CONTINUUM_MCP_TOKEN`), superfícies de apresentação `src/continuum/dashboard/app.py` e `src/continuum/serve/server.py`, CLI `src/continuum/cli/main.py` (`continuum runs`, `continuum tree`, `continuum health`). |
| Substrato de verificação | Dado o checkpoint no tempo T e o mundo como está agora, ainda é seguro e correto continuar? | `src/continuum/state/validator.py` (obsolescência `dependency -> evidence -> finding -> decision` mais `PlanStep.depends_on`), `src/continuum/provenance_map.py` (`Origin` a `REQUIRES_REVIEW` até `REVIEW_CONFIRMED`), `src/continuum/actions/ledger.py` com `src/continuum/actions/idempotency.py` e `src/continuum/gate.py` / `src/continuum/gateway.py` (reivindicar antes de disparar, recusa duplicatas, lança `UnknownSideEffect` para reconciliação), `src/continuum/replayguard.py` (guarda portátil), `src/continuum/pinning.py` e `src/continuum/replay_similarity.py` (correção de reprodução), `src/continuum/budgets.py` (limites de tentativas), `src/continuum/recovery/engine.py` + `src/continuum/recovery/contract.py` + `src/continuum/recovery/planner.py` + `src/continuum/recovery/observations.py` (severidade máxima `RESUME < ... < ABORT`, contrato selado com `evidence` / `reason` / `next_allowed_action` / `human_steps`), `src/continuum/checkpoint/rewind.py` (rebobinamento atômico de estado duplo), `src/continuum/analysis/prefix_trust.py` (confiança consultiva). Verificações publicadas: `docs/recovery_walkthrough.md`, `benchmarks/fault_injection/` (suíte que imprime `detection_rate` / `unsafe_resume_rate`), `src/continuum/benchmark/phase6/` (suíte de correção de recuperação), `docs/RESULTS.md` e o visual regenerável abaixo. |

Cada linha acima é rastreável a um caminho que existe em `main` no commit etiquetado. Nada nesta tabela reexpõe um número de benchmark, os benchmarks só vivem na saída da suíte que já os imprime. Veja `docs/research.md` para a lista completa de suítes publicadas e documentos de design.

### Recuperação de falha, de verdade

A imagem abaixo não é uma maquete. É a saída de `python demo-run/generate_crash_visual.py`, que executa `demo-run/worker.py` até `os._exit(9)` no documento 399, chama `continuum resume --env dataset=v4` e mostra o caminho de recusa (`REQUEST_HUMAN`, `safe:false`, exit 20), reconcilia o efeito colateral incerto com uma sonda, depois retoma a partir do mesmo banco de dados e termina sem trabalho duplicado. A transcrição também é salva como `docs/assets/crash-recovery.txt` para auditoria.

Regenerar:

```bash
python demo-run/generate_crash_visual.py
# ou: python scripts/generate_crash_visual.py
```

![Recuperação de falha: morte brusca no meio do lote, recusa, reconciliação, retomada](docs/assets/crash-recovery.svg)

Passo a passo completo com código em `docs/recovery_walkthrough.md` (`examples/recovery_walkthrough.py`). O harness mínimo de bench está em `references/bench.md` (`continuum benchmark`).

## Funcionalidades

| Capacidade | O que te entrega |
|:--|:--|
| Checkpoints semânticos | Estado compacto, versionado e inspecionável, não um despejo de transcrição |
| Ledger idempotente | Recusa efeitos colaterais externos duplicados, expõe os incertos para reconciliação |
| Revalidação do ambiente | Cada componente do checkpoint é verificado contra o mundo atual antes de retomar |
| Estado com proveniência | Progresso reportado pelo agente é marcado `REQUIRES_REVIEW`, nunca se auto certifica |
| Motor de recuperação | Sete modos de recuperação com um contrato determinístico e selado para a próxima ação |
| Servidor MCP que nega por padrão | Onze ferramentas, separação leitura/mutação, allowlist de chamadores |
| Adaptadores de frameworks | Integrações Python genérico, OpenAI Agents SDK, LangGraph e LangChain |
| Loop de planejamento seguro | Verificação de observações com dois sinais escala ramos de alto risco para REQUIRES_REVIEW |
| Revalidação periódica | Ambiente verificado novamente em agenda, detectando deriva no meio da execução dentro de um ciclo |
| Log à prova de adulteração | Log de eventos encadeado (36 tipos de eventos) com verificação de integridade |
| Porta de cumprimento | Chamadas de efeitos colaterais não reivindicadas são recusadas antes de disparar, mensagens de negação ensinam o protocolo de reivindicação |
| Hooks de observação | Cada arquivo que uma CLI de código escreve se torna evidência verificada por digest, fora do controle do modelo |
| Briefing de sessão | Sessões frescas aprendem o estado da execução de forma determinística no início, incluindo o resumo de raciocínio da sessão anterior |
| Sondas reconciliadoras | Comandos registrados liquidam efeitos colaterais incertos automaticamente, humanos só veem o resto |
| Guia executável | Resume e validate renderizam próximos passos como comandos executáveis, não como estados |
| Gateway HTTP de cumprimento | Chamadas de saída em qualquer linguagem requerem reivindicações, respostas são liquidadas a partir do código de estado real |
| Ponte OpenTelemetry | Spans de chamadas de ferramentas do tracing de produção se tornam evidência sem mudanças de código |
| Índice de ações | Buscas de idempotência entre execuções são leituras indexadas, não varreduras completas |
| Fixação de versões | Hashes de prompt, ferramenta e modelo afirmados pelo chamador são armazenados por reivindicação, deriva aflora ao retomar |
| Orçamentos de tentativas | Limites de tentativas por tipo de ação impostos ao reivindicar, agentes veem tentativas restantes |
| Pai/filho multiagente | Retomada do pai compõe o pior estado da família, filho incerto bloqueia o pai |
| Retentativa informada | Resumos de falha redigidos pelo motor são injetados em retomadas pós recuperação |
| Semântica de bifurcação | Continuações divergentes se ramificam em execuções filhas com autoridade fresca |
| Compactação de log | Prefixo pré-âncora arquivado verbatim, log vivo permanece limitado para execuções de meses |
| Rastreamento de concessões consumidas | Referências de autoridade de uso único são marcadas como gastas em estado terminal, reutilização após restauração é recusada (`GRANT_DENIED`), defendendo o caminho de restauração contra ressurreição de autoridade |
| Atestação de cadeia | `continuum attest` assina a cabeça da cadeia de uma execução com Ed25519 para que um verificador externo possa provar que o histórico não foi adulterado com uma chave conhecida |
| Superfície HITL do dashboard | Botões de confirmar, reconciliar e completar com paridade de auditoria em relação à CLI |

## Extensão de segurança

Duas extensões de segurança aditivas se assentam sobre o substrato de recuperação e checkpoint. Elas não mudam a retomada, a reprodução ou o caminho existente de revalidação no momento da queda.

- **Loop de planejamento seguro**: observações carregam proveniência e são verificadas com dois sinais independentes (`verified` / `unverified` / `contested`). Um ramo do plano protegido por uma observação não verificada ou contestada é escalado para `REQUIRES_REVIEW`. Decisões são acrescentadas ao ledger como eventos `PERCEPTION_OBSERVED` e `BRANCH_RESOLVED`.
- **Revalidação periódica**: reutiliza o motor de recuperação em um intervalo de passos (padrão 25) e na troca de app, de modo que a deriva do ambiente no meio da execução seja capturada dentro de um ciclo em vez de apenas na próxima queda.

Veja [docs/PROBLEM.md](docs/PROBLEM.md), [docs/RESULTS.md](docs/RESULTS.md) e [STATUS.md](STATUS.md).

## Verificação empírica

O CONTINUUM é verificado contra agentes LLM reais, limites de protocolo ao vivo e quedas duras de processo, não apenas testes unitários com mocks.

- **Agentes reais**: lotes de faturas multi sessão com Claude Code com `SIGKILL` no meio da execução, pontuados 7/7 em mecânicas, sessões retomadas consultaram `continuum_resume`, rotearam efeitos colaterais pelo ledger em duas fases, recusaram-se a duplicar escritas verificadas e respeitaram `request_human`. Testes ao vivo expuseram lacunas de deduplicação por deriva de prompt, fechadas com normalização de caminho canônico e fallback baseado em tokens em `ActionLedger.claim()`.
- **Clientes de terceiros**: Gemini CLI e Kilo Code conectados via stdio JSON-RPC contra o armazenamento SQLite ao vivo, validando coexistência multiagente e isolamento de autorização.
- **Conformidade de protocolo**: conduzido de ponta a ponta com `@modelcontextprotocol/inspector --cli` através de mortes de processo, ferramentas mutantes negam por padrão atrás de `CONTINUUM_MCP_MUTATING_CLIENTS`, reivindicações externas degradam para `REQUIRES_REVIEW` (`safe: false`).
- **Auto reparo**: servidores mortos de forma brusca se recuperam de sidecars órfãos `-wal`/`-shm` do SQLite por meio de limpeza de uma única tentativa ao iniciar.
- **Escala**: cerca de 1,380 testes coletados (~1,360 passando, o resto pula sem serviços opcionais) em Python 3.11, 3.12 e 3.13 (unitários, baseados em propriedades com `hypothesis`, concorrência, adversariais). O CONTINUUM-Bench executa cinco cenários de queda mais um cenário dedicado de deriva de argumentos, medindo 0 trabalho duplicado e 0 efeitos colaterais duplicados para o CONTINUUM frente à duplicação total para a reprodução ingênua, mais uma suíte separada de 12 cenários de correção de recuperação (`continuum.benchmark.phase6`) que codifica os pontos de queda do estudo de execução durável como asserções executáveis.
- **Auditoria adversarial**: a superfície MCP completa foi auditada sobre o protocolo ao vivo, três defeitos foram encontrados e corrigidos. Método e passos de reprodução em [test.md](test.md).

## Integração MCP

O CONTINUUM entrega um servidor MCP para que um agente possa registrar progresso, fazer checkpoint e rotear efeitos colaterais externos pelo ledger sem embutir a biblioteca:

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

Onze ferramentas via stdio. Três são somente leitura (`continuum_validate`, `continuum_resume`, `continuum_list_actions`), oito mutam. Efeitos colaterais são em duas fases (reivindicar, executar, completar) e ferramentas mutantes negam por padrão atrás de uma allowlist. Estado reportado pelo agente é registrado com proveniência `Origin.EXTERNAL_AGENT` e marcado `REQUIRES_REVIEW`.

Detalhes de verificação, incluindo recuperação de falha ao iniciar e teste ponta a ponta com Claude Code, em [references/mcp.md](references/mcp.md). Se um servidor registrado reporta `CONNECTION_CLOSED`, a causa quase sempre é a resolução de `PATH` e não o servidor em si: [docs/api/mcp.md](docs/api/mcp.md#troubleshooting) tem o diagnóstico e dois remédios.

## Integração de frameworks

Nove adaptadores são entregues em `src/continuum/adapters/` (uma fachada em processo mais oito integrações), todos instaláveis de forma opcional para que o núcleo permaneça apenas da biblioteca padrão:

| Adaptador | Classe | Notas |
|:--|:--|:--|
| Agente Python genérico | `GenericAgentAdapter` | Fachada em processo, escreve estado confiável (`Origin.DETERMINISTIC`). |
| Sandbox de sistema de arquivos | `FilesystemSandboxAdapter` | Sandbox de diretório local, sem serviço externo, padrão para docs e CI. |
| Python em processo | `PythonInProcAdapter` | Executa Python em um diretório de trabalho temporário, registra via ledger. |
| Contêiner | `ContainerAdapter` | Suportado por Docker, pulo protegido quando `docker` falta. |
| Navegador | `BrowserAdapter` | Suportado por Playwright, pulo protegido quando não instalado. |
| Kubernetes | `KubernetesAdapter` | Suportado por `kubectl`, pulo protegido quando não configurado. |
| OpenAI Agents SDK | `OpenAIAgentAdapter` | Experimental. Engancha `ToolContext` / `RunHooks`, opcional `openai-agents`. |
| LangGraph | `LangGraphAgentAdapter` | Experimental. Envolve um `StateGraph`, opcional `langgraph`. |
| LangChain | `LangChainAgentAdapter` | Experimental. Deixa `checkpoint_node` em um pipeline `Runnable` de LCEL e no loop de chamada de ferramentas de `create_agent`, opcional `langchain`. |

Cada adaptador registra progresso via ledger e roteia efeitos externos pelo protocolo de duas fases de interceptação e completamento. Os três adaptadores de framework têm testes de integração ponta a ponta e foram conduzidos contra um **modelo vivo do OpenRouter**, onde as execuções expuseram e fecharam uma brecha de deduplicação por deriva de argumentos de LLM e dois defeitos do adaptador OpenAI, incluindo um hard crash vivo (`os._exit(137)` no meio do efeito colateral) por adaptador. Uso completo, resultados com modelo vivo e exemplos executáveis para cada adaptador em [references/adapters.md](references/adapters.md).

Apps de produção com LangGraph também podem manter sua API de persistência nativa: `make_continuum_checkpointer(storage)` implementa `BaseCheckpointSaver` do LangGraph sobre o armazenamento do CONTINUUM, de modo que cada put aterrissa no mesmo log de eventos encadeado e com proveniência (ver [references/adapters.md](references/adapters.md)).

Outras três frameworks de produção são cobertos por superfícies finas de hooks sem SDK em [`adapters/thin.py`](src/continuum/adapters/thin.py):

| Framework | Superfície de interceptação | Ponto de entrada |
|:--|:--|:--|
| CrewAI | hooks globais antes/depois de chamada de ferramenta | `install_crewai_hooks(storage, run_id)` |
| AutoGen core | `FunctionTool.run_json` envolvido no local | `wrap_autogen_tool(tool, storage, run_id)` |
| Pydantic AI | capacidade assíncrona de Hooks | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` |

Para stacks que nenhum destes alcança: `continuum gateway` impõe reivindicações em HTTP de saída de qualquer linguagem, `continuum.otel.make_span_processor(storage)` converte spans existentes de OpenTelemetry de ferramentas em evidência, e `continuum serve` expõe as mesmas operações que as ferramentas MCP sobre um protocolo de fio JSON agnóstico à linguagem (stdio, ou HTTP via `--transport http` com autenticação `CONTINUUM_SERVE_TOKEN`).

### Retomando execuções reportadas por agente ou MCP

Estado reportado via MCP, ou através do adaptador OpenAI, carrega proveniência `Origin.EXTERNAL_AGENT` e se resolve para `request_human` até ser confirmado. Execuções de LangGraph e LangChain usam `Origin.DETERMINISTIC` e retomam diretamente. Para limpar a revisão e retomar:

```bash
continuum confirm <run_id>   # registra REVIEW_CONFIRMED, depois reavalia
continuum resume <run_id>    # agora reporta RESUME
```

Sobre MCP o equivalente é a ferramenta `continuum_confirm` seguida de `continuum_resume`. A confirmação é um evento único e atestado por humano: a escotilha de escape para a segurança de auto certificação, de modo que uma execução dirigida externamente nunca fica presa permanentemente.

## Conceitos centrais

A referência profunda para cada conceito vive em [references/concepts.md](references/concepts.md).

- **Checkpoints semânticos**, uma representação compacta e versionada do que o agente precisa para continuar.
- **Validação de estado**, cada componente verificado de forma independente, obsolescência se propaga pelo grafo de dependências.
- **Ledger idempotente**, efeitos colaterais externos são rastreados e deduplicados, resultados incertos lançam em vez de tentar novamente silenciosamente.
- **Modos de recuperação**, `RESUME`, `REPAIR_AND_RESUME`, `ROLLBACK`, `WAIT`, `REQUEST_HUMAN`, `ABORT` (mais `REPLAN`).
- **Contrato de recuperação**, uma próxima ação determinística, selada por integridade e protegida.

## Arquitetura

O CONTINUUM se organiza em torno de um invariante: **cada fato carrega sua origem, e a confiança é conquistada, nunca assumida.** Por que isso importa para uma startup: um agente que roda por semanas não deve perder trabalho quando seu contexto se perde, e não deve desperdiçar tokens, custo ou disparar uma ferramenta duas vezes.

### Sistema em um relance, adaptador universal, um log, qualquer harness

Qualquer harness se conecta ao mesmo log encadeado. A mesma execução pode ser escrita pelo Claude Code, retomada pelo LangGraph, inspecionada pela CLI e aprovada no dashboard. Nenhuma cooperação de framework é necessária.

```text
  Claude Code ─┐
  Gemini CLI ──┤
  Codex ───────┤
  LangGraph ───┼── 5 costuras ──►  Um log durável  ──►  Recuperação + Dashboard + CLI
  LangChain ───┤                (encadeado,        (contrato selado,
  OpenAI SDK ──┤                 com proveniência,   verificação, saúde,
  CrewAI ──────┤                 exatamente uma vez)  família)
  Qualquer HTTP ──┤
  Qualquer app OTel ┘

  Costuras: 1 Em processo  2 MCP  3 Hooks CLI  4 Gateway  5 OTel
```

### As três garantias (a demo prova cada uma)

1. **Sem auto certificação.** Estado reportado pelo agente é `EXTERNAL_AGENT` e degrada para `REQUIRES_REVIEW` até um `REVIEW_CONFIRMED` humano. Apenas escritores confiáveis produzem estado `DETERMINISTIC`.
2. **Efeitos colaterais requerem reivindicações.** Cada efeito externo é reivindicado em um ledger idempotente antes de disparar. Efeitos não reivindicados são bloqueados no limite, duplicatas são recusadas, resultados incertos são elevados para reconciliação.
3. **Recuperação verifica contra a realidade.** A retomada verifica digests de arquivos, versões de dependências e identidade do modelo antes de dizer que é seguro. A obsolescência se propaga `dependency -> evidence -> finding -> decision` mais `PlanStep.depends_on` de modo que apenas os passos afetados se reparam.

### Cinco costuras de integração

| Costura | Como conectar | O que te entrega |
|:--|:--|:--|
| 1 Em processo | `GenericAgentAdapter.intercept_action(...)` e `wrap_tool(key_fn=...)` em LangChain, LangGraph, OpenAI Agents SDK | Frameworks Python, escritas confiáveis |
| 2 Servidor MCP | `continuum-mcp` 12 ferramentas via stdio (`continuum_record_progress`, `continuum_intercept_action`, `continuum_complete_action`, etc.) | Qualquer cliente capaz de MCP, 3 somente leitura + 8 mutantes, allowlist `CONTINUUM_MCP_MUTATING_CLIENTS` |
| 3 Hooks de ciclo de vida CLI | `continuum hooks install claude-code --with-gate` também `gemini` e `codex` | CLIs de código: `SessionStart briefing`, `PostToolUse observe`, `PreToolUse gate`, sem necessidade de CLAUDE.md |
| 4 Gateway HTTP de cumprimento | `continuum gateway --port 8765` com `.continuum/gateway.json` | Qualquer linguagem, qualquer HTTP de saída deve ter uma reivindicação, o gateway liquida a partir do código de estado real |
| 5 Ponte OpenTelemetry | `make_span_processor(storage)` | Qualquer app rastreada, spans se tornam evidência `TOOL_COMPLETED` |

Superfícies finas de hooks para CrewAI, AutoGen, Pydantic AI vivem em `adapters/thin.py` sem necessidade de SDK.

### Pipeline de cumprimento, por que sem duplicatas e sem chamadas inválidas

O pipeline de porta para observação fecha a lacuna no limite do harness. Isso é o que economiza tokens e custo e bloqueia chamadas inválidas de ferramentas.

```text
Hook PreToolUse                    Hook PostToolUse
    |                                    |
    v                                    v
continuum gate                    continuum observe
    |                                    |
    |-- sem reivindicação? NEGA (exit 2)          |-- evento TOOL_COMPLETED:
    |   + instruções para reivindicar          |     caminho, bytes, sha256 em disco agora
    |                                    |
    |-- reivindicação viva? PERMITE                |-- estado verificado em disco:
    |                                    |     verificado / alterado / faltando
    v
agente executa o efeito
    |
    v
continuum_complete_action  (liquidado a partir da realidade, não do relatório)
    |
    v
ledger marcado COMPLETADO, próxima reprodução devolve resultado em cache, não um segundo disparo
```

Host desconhecido é negado com falha fechada, não como relay aberto. Shell `Bash/curl` é o ponto cego documentado da v1.

### Árvore de decisão de recuperação, semanas até terminar, correto e exato

O motor toma o sinal mais cauteloso, de modo que a segurança nunca perde para a conveniência.

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

Cada `continuum resume` devolve um contrato selado com: estado de recuperação e `safe`, componentes verificados e invalidados, `human_steps` executáveis (shell exato a executar), observações pós checkpoint verificadas em disco, deriva de fixação e agregação familiar para `continuum tree` multiagente. O briefing `continuum briefing` injeta esse contrato em cada `claude` SessionStart fresco, de modo que dizer `oi` depois de matar o terminal retoma a partir do último prefixo bom.

### Por que isso economiza tokens, custo e chamadas inválidas

* **Tokens:** Checkpoint semântico armazena `Goal + Plan + Progress` não um despejo de transcrição. O briefing serve apenas estado verificado mais um resumo de raciocínio com teto de 4096, não a cauda de erros que o auto condicionamento mostra que degrada a próxima sessão. A retentativa informada `recovery/summary.py` injeta um resumo redigido pelo motor, não histórico bruto.
* **Custo:** O ledger `action_index` recusa efeitos colaterais duplicados mesmo sob deriva de argumentos como caminhos relativos versus absolutos (`invoice:INV-001` chave estável), de modo que a mesma API não é paga duas vezes após uma retomada. Orçamentos `budgets.py` limitam tempestades de retentativas ao reivindicar. `continuum benchmark` imprime `0 duplicatas` para o continuum versus `50` para o ingênuo.
* **Chamadas inválidas:** A porta, o gateway e `replayguard` com `langgraph_protected_node` bloqueiam chamadas de ferramentas não reivindicadas ou reproduzidas antes de disparar. A fixação `pinning.py` expõe deriva de prompt ou ferramenta ao retomar.

### Arquitetura de armazenamento

Esquema v6. SQLite é primário, Postgres verificado por CI. Um log, muitas projeções.

| Tabela | Propósito |
|:--|:--|
| `events` | Log somente anexado encadeado (44 tipos de eventos em v0.2) |
| `runs` | Metadados de execução com `parent_run_id` para multiagente |
| `versions` | Instantâneos de SemanticState por checkpoint |
| `checkpoints` | Registros de checkpoint selados com âncoras `RECOVERY` |
| `action_index` | Projeção de idempotência entre execuções (schema v3+), leituras indexadas, não varreduras completas |
| `events_archive` | Armazenamento de prefixo compactado (schema v5+), `continuum compact` limita o log vivo para execuções de meses |
| `lg_checkpoints` / `lg_writes` | Persistência nativa do LangGraph (schema v4+), `make_continuum_checkpointer(storage)` |

### Mapa de módulos, uma biblioteca, muitas superfícies

O CONTINUUM é uma biblioteca (`src/continuum`, 104 módulos) mais uma suíte de testes grande (98 arquivos de teste, ~1,380 testes). Todos os módulos acrescentam e reproduzem um log de eventos encadeado:

| Módulo | Papel |
|:--|:--|
| `events.py` | Log somente anexado encadeado e `verify() trusted_through` |
| `state/` | Projeção `project()`, validação, extração, propagação de obsolescência |
| `storage/` | `SQLiteStorage` v6, `postgres.py`, `migrations.py`, `actionindex.py` |
| `actions/` | Ledger idempotente `claim/complete/reconcile`, `idempotency.py` chave e canonização e fallback por token, rastreamento de concessão consumida `GRANT_DENIED` |
| `checkpoint/` | Checkpoints orientados por políticas `manager.py` `policy.py` com âncoras `RECOVERY` e `prune` |
| `recovery/` | Motor, planejador, contrato selado `contract.py`, `guidance` `human_steps`, `observations` verificadas em disco, `family` rollup, `fork` semântica, `summary` retentativa informada |
| `gate.py` | Cumprimento antes da ferramenta: permitir ou negar contra reivindicações do ledger |
| `gateway.py` | Proxy HTTP de cumprimento: reivindicar antes de disparar para solicitações de saída |
| `replayguard.py` | Guarda portátil: `evaluate, protected_call, langgraph_protected_node`, fecha risco de reprodução do ACRFence |
| `hooks.py` `clienthooks.py` | Hooks de checkpoint compartilhados e perfis de instalador `claude-code gemini codex` |
| `budgets.py` | Registro e avaliação de orçamento de tentativas por tipo de ação |
| `pinning.py` | Normalização de fixação de versões e detecção de deriva ao retomar |
| `replay_similarity.py` | Backends de similaridade semântica exact/fuzzy/embedding para reprodução vs bifurcação |
| `reconcilers.py` | Registro de sondas `.continuum/reconcilers.json` para liquidação automática |
| `adapters/` | 9 adaptadores de classe + hooks finos `thin.py` CrewAI AutoGen Pydantic AI + armazém LangGraph |
| `mcp/` | 12 ferramentas stdio mais autorização `authz.py` autenticação por token, allowlist, token de confirmação |
| `serve/` | Sidecar stdio fio JSON + HTTP `CONTINUUM_SERVE_TOKEN` |
| `dashboard/` | Dashboard web `app.py` `hitl.py` com botões HITL confirmar/reconciliar/completar, aviso de confiança de prefixo, fixações |
| `cli/` | 38 comandos argparse, códigos de saída como veredito, `runs, start, inspect, resume, verify, health, tree, benchmark, attest, dashboard` |
| `otel.py` | Ponte de processador de spans do OpenTelemetry |
| `benchmark/` | Harness do CONTINUUM-Bench, 5 cenários de queda + deriva de argumentos + suíte de recuperação de 12 cenários |

### Limitações honestas

- A porta não enxerga dentro de comandos shell (Bash/curl contorna reivindicações de ferramentas estruturadas)
- Backend de Postgres testado por CI mas não curtido em produção
- Ainda não há webhook de saída para notificações `request_human` (#305)
- Um nível de hierarquia multiagente na v1
- Offloading de payloads grandes (#254) ainda não implementado
- Benchmark em escala de semanas com tabela de custo de tokens cai no quadro #550 (#568 a #570)

Referência completa em [references/architecture.md](references/architecture.md). E o plano de meses que se constrói sobre isso, grafo causal de proveniência, ressurreição de autoridade, admissibilidade, vivacidade, está fixado como quadro #550 com 20 sub issues #551 a #570.

## API e CLI

Superfície Python (`EventType`, `Run`, `SQLiteStorage`, `diff_states`, `project`) e a API de adaptadores estão documentadas com exemplos executáveis em [references/api.md](references/api.md). A CLI é a mesma superfície em forma de shell:

```bash
continuum runs                                   # listar execuções
continuum inspect <run_id>                       # estado semântico
continuum validate <run_id> --env dataset=v4     # validar, somente leitura
continuum resume <run_id> --env dataset=v4       # decisão de recuperação + contrato + próximos passos
continuum checkpoint <run_id>                    # forçar um checkpoint, muta
continuum actions <run_id>                       # efeitos colaterais externos
continuum reconcile <run_id>                     # liquidar efeitos incertos com sondas
continuum complete <run_id>                      # fechar uma execução como feita, do teclado
continuum verify <run_id>                        # reauditar a cadeia de hash de eventos
continuum budget <run_id>                        # uso de orçamento de tentativas por tipo de ação
continuum compact <run_id>                       # arquivar prefixo de log pré-âncora
continuum tree <parent_run_id>                   # mostrar pai + filhos com estados de recuperação
continuum attest <run_id> --key signer.pem       # assinar a cabeça da cadeia para um verificador externo
```

Toda a fiação está do lado do host, a cooperação do modelo é opcional:

```bash
continuum hooks install claude-code --with-gate   # CLIs de código: evidência, briefing, porta
continuum gateway --port 8765                     # proxy HTTP de cumprimento para todo o resto
provider.add_span_processor(continuum.otel.make_span_processor(storage))  # OTel para evidência
continuum-mcp                                     # qualquer coisa capaz de MCP: o servidor de onze ferramentas
continuum briefing                                # injeção de contexto no início da sessão
continuum budget <run_id>                        # relatório de uso de orçamento de tentativas
continuum tree <parent_run_id>                   # visão de hierarquia multiagente
```

Registros opcionais vivem ao lado do seu código e são dados, não código: `.continuum/gate.json` (ferramentas de efeito colateral + templates de chave estável), `.continuum/reconcilers.json` (sondas que checam sistemas externos), `.continuum/gateway.json` (rotas upstream).

Cada comando aceita `--json`, e comandos somente leitura nunca escrevem, de modo que são seguros contra um banco de dados vivo enquanto um agente está no meio da execução. Códigos de saída são um contrato de segurança (apenas uma execução verificada segura sai com 0). Lista completa de comandos, tabela de códigos de saída e saída de diff de estado em [references/cli.md](references/cli.md).

## Roteiro

| Fase | Componente | Estado |
|:--:|:--|:--|
| 1-11 | Modelos de dados, estado semântico, persistência, checkpointing, validação, ledger de ações, motor de recuperação, CLI, exemplos de recuperação de falha, snapshots e diffs de ambiente, adaptadores de framework | Completo |
| 12 | Suíte de benchmarks (CONTINUUM-Bench) | Completo (harness mínimo) |
| 13 | API na nuvem (FastAPI + PostgreSQL) | Parcial: o backend de armazenamento PostgreSQL e o transporte sidecar HTTP (`continuum serve --transport http`) estão entregues e testados por CI, o serviço multi-tenant hospedado não foi iniciado |
| 14 | Dashboard | Completo (`continuum dashboard`) |
| 15+ | Durabilidade imposta: hooks de observação, porta, briefing de sessão, sondas reconciliadoras, gateway de cumprimento, ponte OTel, índice de ações, guia executável, instaladores multi cliente, detecção de reprodução semântica, fixação de versões, orçamentos de tentativas, compactação de log, superfície HITL, semântica de bifurcação, retentativa informada, agregação multiagente | Completo (ver issue #213) |
| Próximo | Plano de durabilidade em escala de meses: planos ancorados em marcos (#312), memória de tentativas estruturada (#313), rebobinamento atômico de estado duplo (#292), benchmark público de correção de recuperação (#293), notificações de saída por webhook (#305) | Planejado (rascunho da especificação em [docs/UPGRADE_SPEC.md](docs/UPGRADE_SPEC.md)) |

Além do plano original: o servidor MCP, as camadas de autorização MCP e autenticação de chamador, proveniência e anti auto certificação, arquivos de comunidade, versionamento de esquema com migrações para frente, um contexto de recuperação limitado, rastreamento de concessões consumidas, atestação de cadeia de eventos Ed25519, o checkpointer nativo do LangGraph e artefatos wheel em cada push para `main` estão entregues. Veja [STATUS.md](STATUS.md) para a divisão verificado versus acreditado e os defeitos de correção abertos.

## O que o CONTINUUM não é

| Não é isto | Em vez disso é isto |
|:--|:--|
| Um LLM | Uma camada de confiabilidade para agentes que usam LLMs |
| Um framework de agentes | Uma camada de recuperação que se conecta a qualquer framework |
| Um banco de dados vetorial | Estado semântico estruturado, não embeddings |
| Um sistema RAG | Checkpoints verificados, não memória aumentada por recuperação |
| Um motor de fluxo de trabalho | Uma camada de recuperação, não um orquestrador |

A abstração central: `estado semântico + validação do ambiente + reconciliação de ações = recuperação segura`.

## Trabalho relacionado

O CONTINUUM se situa na interseção de execução durável, rastreamento idempotente de efeitos colaterais e recuperação de falha para agentes LLM. Os vizinhos mais próximos são contratos de retomada verificados por máquina (Khan 2026), processamento transacional agêntico com admissão protegida por restrições (Mnemosyne 2026), análise de ataques de reversão de checkpoint (ACRFence 2026) e defesa de injeção de prompt em nível de design (CaMeL 2025). A lista completa anotada, fundamentos e auditoria de citações estão em [references/related-work.md](references/related-work.md).

## Status e limitações

- **Testado**: 1,360 passados + 23 pulados em uma execução completa na auditoria de 2026-08-24 desta árvore, CI impõe a suíte em Python 3.11, 3.12 e 3.13, e as contagens variam por plataforma e serviços opcionais como Postgres (ver [STATUS.md](STATUS.md)). A superfície MCP também foi auditada de forma adversarial sobre o protocolo ao vivo, ver [test.md](test.md).
- **No PyPI como `continuum-agent` 0.1.0** (`pip install continuum-agent`, o clone ainda funciona via `pip install .` ver Início rápido).
- **Autenticação de chamador MCP é opcional por implantação.** Quando `CONTINUUM_MCP_TOKEN` é definido, o servidor recusa cada ferramenta mutante a menos que o chamador apresente esse segredo compartilhado no `_meta.authToken` do handshake `initialize`, segredos por chamador disponíveis via `CONTINUUM_MCP_CLIENT_TOKENS` (pares `name:secret`). Sem nenhum token configurado, a autorização é apenas por identidade declarada (o valor histórico padrão, preservado para uso local de usuário único).
- **Confirmar estado auto reportado via MCP requer um segredo separado.** `continuum_confirm` recusa cada chamador até que o operador defina `CONTINUUM_MCP_CONFIRM_TOKEN`, porque um agente com permissão para registrar progresso não deve também ter permissão para confirmá-lo. O caminho padrão permanece conduzido por humano: execute `continuum confirm <run_id>` no host.
- **Componentes não construídos**: API na nuvem (Fase 13).
- **Lacuna de cumprimento em comandos shell**: a porta impõe reivindicações para chamadas de ferramentas estruturadas mas não consegue ver dentro de comandos Bash ou curl. Documentado como escopo v1 recusado.
- **Adaptadores de framework permanecem experimentais.** Os três adaptadores de framework agora carregam provas de retomada suave e queda dura com modelo vivo (OpenRouter, `gpt-4o-mini`), incluindo a prova de contrato de queda que bloqueia retomada sobre um efeito colateral incerto, e agora têm testes de verificação de queda e retomada que alcançam paridade com a fachada genérica (Refs #285). Prefira `GenericAgentAdapter` para recuperação em produção.
- **Execuções de agente e MCP precisam de confirmação explícita antes da retomada automática.** Estado reportado externamente é `REQUIRES_REVIEW`, portanto `continuum resume` retorna `request_human` até que um humano confirme. Por design, não é um defeito, ver [Integração de frameworks](#integração-de-frameworks).
- **Série de testes de autonomia e2e** (issue [#6](https://github.com/Cyrax321/CONTINUUM/issues/6)): três execuções completas do Claude Code pontuaram 7/7 em mecânicas com comportamento de recuperação não solicitado observado. Mais iterações através de diversos estilos de prompt permanecem abertas.

## Sobre

No início de 2026 vi agentes de longa duração falharem na recuperação, não no raciocínio. Checkpoints eram tratados como prova para continuar, não como evidência a verificar. Pesquisando Temporal, LangGraph, ACRFence 2603.20625 e self conditioning 2509.09677, encontrei que a lacuna era um substrato de verificação portátil que pergunta, dado o estado no tempo T e o mundo como está agora, ainda é seguro continuar.

Em três semanas construí o CONTINUUM a partir de um invariante, cada fato carrega sua origem. O resultado é um log encadeado com `verify()`, um ledger com deduplicação por chave estável, uma porta e um gateway que bloqueiam efeitos não reivindicados, e um motor de recuperação que sela um contrato. Cinco costuras expõem o mesmo log ao Claude Code, LangGraph, LangChain, OpenAI, HTTP e OpenTelemetry. Validado com mortes reais e 1380 testes, ele imprime `0 duplicatas` onde a reprodução ingênua imprime `50`.

O CONTINUUM foi criado por **Anandhu P Shaji** ([@Cyrax321](https://github.com/Cyrax321) · [LinkedIn](https://www.linkedin.com/in/anandhupshaji/)) e é mantido pelo criador original. É de código aberto sob [Apache-2.0](LICENSE). Contribuições da comunidade são bem-vindas via [CONTRIBUTING.md](CONTRIBUTING.md) e são creditadas em [AUTHORS.md](AUTHORS.md) e [graphs/contributors](https://github.com/Cyrax321/CONTINUUM/graphs/contributors).

## Contribuir

Este projeto é de código aberto sob Apache 2.0 e deliberadamente construído para ser estendido: por pesquisadores validando a semântica de recuperação, por engenheiros portando o ledger ou o servidor MCP para outros frameworks ou linguagens, e por qualquer pessoa que transforme o roteiro planejado em realidade. Um bom lugar para começar é a etiqueta `good first issue` no [rastreador de issues](https://github.com/Cyrax321/CONTINUUM/issues), ou os defeitos abertos de correção listados em STATUS.md.

Abra uma issue antes de enviar PRs grandes. Veja [CONTRIBUTING.md](CONTRIBUTING.md) para o guia completo de contribuição, incluindo o [Code of Conduct](CODE_OF_CONDUCT.md).

### Colaboradores

<a href="https://github.com/Cyrax321/CONTINUUM/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Cyrax321/CONTINUUM" />
</a>

## Patrocinar

Se o CONTINUUM ajuda seus agentes a se recuperarem de forma confiável, considere patrocinar para apoiar a manutenção a longo prazo.

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/Sponsor-❤-ff69b4?style=for-the-badge&logo=githubsponsors" alt="Sponsor Cyrax321" /></a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321">Torne-se um patrocinador</a>, GitHub Sponsors, ou adicione um link personalizado em FUNDING.yml se preferir outra plataforma.
</p>

## Licença

Apache 2.0, ver [LICENSE](LICENSE).

---

Material de referência profundo:

- [references/install.md](references/install.md) - pré-requisitos, níveis de instalação, mapa de pacotes, verificação
- [references/concepts.md](references/concepts.md) - checkpoints semânticos, validação, ledger, modos de recuperação, contrato
- [references/architecture.md](references/architecture.md) - modelo de dados, log de eventos, projeção, armazenamento, checkpointing, motor de recuperação, segurança
- [references/adapters.md](references/adapters.md) - uso de adaptadores de framework e resultados de validação com modelo vivo
- [references/api.md](references/api.md) - API Python e adaptadores
- [references/cli.md](references/cli.md) - lista completa de comandos CLI, códigos de saída, diff de estado
- [references/mcp.md](references/mcp.md) - estado do servidor MCP, verificação, perguntas abertas
- [references/bench.md](references/bench.md) - design do CONTINUUM-Bench
- [references/quickstart.md](references/quickstart.md) - instalação, exemplos, os scripts de prova
- [references/e2e.md](references/e2e.md) - passo a passo de teste de autonomia ponta a ponta
- [references/testing.md](references/testing.md) - disposição e convenções da suíte de testes
- [references/related-work.md](references/related-work.md) - trabalho relacionado anotado e auditoria de citações
