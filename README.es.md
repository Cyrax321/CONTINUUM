<p align="center">
  <img src="docs/assets/readme-img.png" alt="Banner de CONTINUUM" width="100%" />
</p>

<p align="center">
  <strong>CONTINUUM: Recuperación semántica verificable para agentes de IA de larga duración.</strong>
  Checkpoints semánticos (no volcados de conversación), un libro mayor idempotente de acciones
  que rechaza efectos secundarios duplicados, y un registro de eventos encadenado y a prueba de manipulaciones,
  todo expuesto como un servidor MCP que deniega por defecto. Agnóstico al framework, Python 3.11+.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://pypi.org/project/continuum-agent/"><img src="https://img.shields.io/pypi/v/continuum-agent?style=flat-square&label=PyPI" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square" alt="License" /></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/pydantic-v2-E92063?style=flat-square&logo=pydantic&logoColor=white" alt="Pydantic v2" /></a>
  <a href="https://continuum-nu-six.vercel.app/"><img src="https://img.shields.io/badge/website-live_demo-E06D53?style=flat-square" alt="Website Demo" /></a>
  <a href="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml"><img src="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml/badge.svg" alt="Estado CI" /></a>
  <a href="https://app.codecov.io/gh/Cyrax321/CONTINUUM"><img src="https://img.shields.io/codecov/c/github/Cyrax321/CONTINUUM?style=flat-square&logo=codecov" alt="Coverage" /></a>
</p>

<p align="center" style="margin-bottom: 6px;">
  <a href="https://continuum-nu-six.vercel.app/"><strong>Visita el sitio web de CONTINUUM</strong></a>
</p>

<p align="center" style="margin-top: 6px;">
  <a href="https://app.ona.com/#https://github.com/Cyrax321/CONTINUUM"><img src="https://ona.com/build-with-ona.svg" alt="Build with Ona" /></a>
</p>

<p align="center">
  <sub>Si CONTINUUM ayuda a tus agentes a recuperarse, por favor dale una estrella al repositorio. Ayuda a otros a descubrirlo y mantiene las buenas first issues llegando.</sub>
</p>

<p align="center">
  <sub><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <strong>Español</strong> | <a href="README.ja.md">日本語</a> | <a href="README.pt-BR.md">Português</a> | <a href="README.ko.md">한국어</a></sub>
</p>

---

## Contenidos

[Por qué](#por-qué) · [Inicio rápido](#inicio-rápido) · [Cómo funciona](#cómo-funciona) · [Dónde se sitúa CONTINUUM](#dónde-se-sitúa-continuum) · [Características](#características) · [Extensión de seguridad](#extensión-de-seguridad) · [Verificación empírica](#verificación-empírica) · [Integración MCP](#integración-mcp) · [Integración de frameworks](#integración-de-frameworks) · [Conceptos clave](#conceptos-clave) · [Arquitectura](#arquitectura) · [API y CLI](#api-y-cli) · [Hoja de ruta](#hoja-de-ruta) · [Lo que CONTINUUM no es](#lo-que-continuum-no-es) · [Trabajo relacionado](#trabajo-relacionado) · [Estado y limitaciones](#estado-y-limitaciones) · [Contribuir](#contribuir) · [Licencia](#licencia)

---

## Por qué

Los agentes de IA modernos ejecutan tareas largas, con cientos de llamadas LLM, invocaciones de herramientas y escrituras en archivos y bases de datos. Cuando fallan, la respuesta habitual es reproducir todo desde cero, lo que duplica trabajo, duplica efectos secundarios, malgasta tokens y pierde decisiones.

CONTINUUM plantea una pregunta más precisa y más difícil: puede un agente reanudarse desde una representación semántica compacta de su estado de tarea mientras verifica de forma independiente que ese estado sigue siendo válido en el entorno actual? Su diferenciador tiene tres partes:

- **Checkpoints semánticos**: una representación compacta y versionada de lo que el agente necesita para continuar, no un volcado de conversación.
- **Revalidación independiente del entorno**: cada componente del checkpoint se verifica contra el entorno actual antes de reanudar, y la obsolescencia se propaga por el grafo de dependencias.
- **Estado con procedencia**: cada hecho lleva su origen, por lo que el progreso reportado por el agente nunca se auto certifica.

## Inicio rápido

Publicado en PyPI como `continuum-agent` 0.1.0, ejecuta `pip install continuum-agent` (`pip install continuum-agent==0.1.0` para fijar la versión). Las etiquetas de release además adjuntan wheels construidos en [GitHub Releases](https://github.com/Cyrax321/CONTINUUM/releases).

Rutas sin configuración (sin clonar, sin instalar, sin publicar nada):

| Ruta | Cómo |
|:--|:--|
| Instalar desde PyPI | `pip install continuum-agent==0.1.0` y luego `continuum --help` |
| Ver la recuperación tras fallo de principio a fin | `docker run --rm ghcr.io/cyrax321/continuum` |
| Usar la CLI a través de Docker | `docker run --rm ghcr.io/cyrax321/continuum continuum --help` |
| Ejecutar la CLI sin clonar | `uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help` |
| Windows PowerShell (desde un clon) | `powershell -ExecutionPolicy Bypass -File .\try-it.ps1` o `powershell -ExecutionPolicy Bypass -File .\try-it.ps1 cli --help` |
| Entorno de desarrollo completo en el navegador | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Cyrax321/CONTINUUM?quickstart=1) |

La imagen Docker se publica en GHCR por CI en cada push a `main` y en cada etiqueta de release (`.github/workflows/docker-publish.yml`). El Codespace se define en `.devcontainer/`.

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

uv venv && source .venv/bin/activate     # macOS / Linux; Windows: .venv\Scripts\activate

# Colaboradores (recomendado): librería + CLI + todas las herramientas de test + cada adaptador
uv pip install -e ".[dev]"

# O elige solo lo que necesitas: . (mínimo), [mcp], [otel], [langgraph],
# [openai], [langchain], [attest], [postgres]

# O sáltate el clon por completo:
uv pip install git+https://github.com/Cyrax321/CONTINUUM.git
uv pip install "continuum-agent[mcp] @ git+https://github.com/Cyrax321/CONTINUUM.git"
```

> **Alternativa con pip:** reemplaza `uv pip install` por `pip install` en cada comando anterior.

Verifica:

```bash
continuum --help                 # punto de entrada CLI
continuum-mcp --help             # punto de entrada servidor MCP (necesita [mcp] o [dev])
pytest -q                        # ~1,380 tests recogidos (el número exacto y los saltos varían por entorno)
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum               # las tres puertas que CI exige
```

La librería central tiene una sola dependencia de runtime (`pydantic>=2.7`), todo lo demás es opcional. El mapa completo de paquetes, la matriz de extras, la configuración de tests de Postgres y la verificación por comando están en [references/install.md](references/install.md).

### Conecta un agente de código en dos minutos

Para Claude Code, Gemini CLI o Codex, no escribes Python y no necesitas archivo de prompts:

```bash
continuum start my-task --goal "Qué debe hacer el agente"
continuum hooks install claude-code --with-gate   # también: gemini, codex
```

Desde entonces cada archivo que el agente escribe se captura como evidencia encadenada, su sesión arranca con un briefing automático de estado, los efectos secundarios no declarados registrados en `.continuum/gate.json` se rechazan antes de ejecutarse, y una sesión fresca tras cualquier caída se reanuda con pasos siguientes ejecutables. No se necesita CLAUDE.md.

Ejemplo mínimo de librería, registro y recuperación:

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="Analizar 10,000 documentos"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "Analizar 10,000 documentos", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# Tras una caída, un proceso nuevo retoma exactamente donde se detuvo:
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # ya hecho, no se repite
print(store.verify_events("run_4821").ok)  # True, cadena intacta tras la caída
```

**Ejecuta la prueba tú mismo:**

```bash
python examples/crash_recovery_agent.py   # muerte real del proceso, efecto secundario real
python examples/context_compaction.py     # transcripción perdida, checkpoint sobrevive
python examples/model_switch.py           # Modelo A muere, Modelo B retoma de forma segura
python scripts/mcp_smoke.py               # subproceso real, tráfico JSON-RPC real
```

El kit `e2e-autonomy-test/` guioniza una tarea real de lotes de facturas, una muerte brusca a mitad de ejecución y una sesión fresca de reanudación, luego puntúa el outbox, el libro mayor y la cadena de eventos fuera de banda. La ejecución 1 obtuvo **7/7 en mecánicas** contra una sesión real de Claude Code. Recorrido completo en [references/e2e.md](references/e2e.md).

## Cómo funciona

CONTINUUM separa el **contexto LLM** (temporal) del **estado duradero de la tarea** (permanente). En lugar de guardar el historial de conversación, construye un checkpoint semántico, la mínima información verificada necesaria para continuar.

![Cómo funciona CONTINUUM](docs/assets/architecture.svg)

La explicación detallada, el modelo de proyección y el contexto de recuperación están en [references/architecture.md](references/architecture.md).

## Dónde se sitúa CONTINUUM

Cuatro preocupaciones se solapan en cada agente de larga duración. CONTINUUM solo es dueño de la última y toca las otras tres a través de costuras explícitas. No se nombra a ningún competidor y no se hace ninguna afirmación sin un módulo entregado o una suite publicada que ya lo imprima.

| Capa | Responde | Cómo se conecta (módulos entregados o salidas publicadas) |
|:--|:--|:--|
| Harness | Cómo el agente llama a herramientas y avanza hacia un objetivo? | Fuera de CONTINUUM. Puntos de conexión entregados en `src/continuum/adapters/generic.py` (`GenericAgentAdapter`), `src/continuum/adapters/thin.py` (hooks de CrewAI, AutoGen, Pydantic AI), `src/continuum/mcp/server.py` (MCP stdio), `src/continuum/hooks.py` y `src/continuum/clienthooks.py` (hooks de ciclo de vida de CLI de código), `src/continuum/gateway.py` (proxy HTTP de cumplimiento para cualquier lenguaje) y `src/continuum/otel.py` (puente OpenTelemetry). Recetas en `docs/recipes/` y `references/adapters.md`. |
| Ejecución durable | Qué pasó antes de una caída y qué puede reproducirse sin perder trabajo? | Registro de eventos encadenado `src/continuum/events.py` con `verify()` y `trusted_through`, almacenamiento durable `src/continuum/storage/sqlite.py` (WAL, `synchronous=FULL`, schema v6) y `src/continuum/storage/postgres.py` más `src/continuum/storage/migrations.py`, checkpoints dirigidos por políticas `src/continuum/checkpoint/manager.py` y `src/continuum/checkpoint/policy.py` que reproducen el hueco en `restore()`. Recorrido en `docs/recovery_walkthrough.md` (salida de `examples/recovery_walkthrough.py`). |
| Plano de control | Qué ejecución está activa, quién puede actuar sobre ella y a dónde va la salida? | Registro de ejecuciones y jerarquía padre/hijo `src/continuum/storage/` y `src/continuum/recovery/family.py` (`continuum tree`), autorización allowlist `src/continuum/mcp/authz.py` (`CONTINUUM_MCP_MUTATING_CLIENTS` / `CONTINUUM_MCP_TOKEN`), superficies de presentación `src/continuum/dashboard/app.py` y `src/continuum/serve/server.py`, CLI `src/continuum/cli/main.py` (`continuum runs`, `continuum tree`, `continuum health`). |
| Sustrato de verificación | Dado el checkpoint en el tiempo T y el mundo tal como está ahora, sigue siendo seguro y correcto continuar? | `src/continuum/state/validator.py` (obsolescencia `dependency -> evidence -> finding -> decision` más `PlanStep.depends_on`), `src/continuum/provenance_map.py` (`Origin` a `REQUIRES_REVIEW` hasta `REVIEW_CONFIRMED`), `src/continuum/actions/ledger.py` con `src/continuum/actions/idempotency.py` y `src/continuum/gate.py` / `src/continuum/gateway.py` (reclamar antes de ejecutar, rechaza duplicados, lanza `UnknownSideEffect` para reconciliación), `src/continuum/replayguard.py` (guardia portable), `src/continuum/pinning.py` y `src/continuum/replay_similarity.py` (corrección de reproducción), `src/continuum/budgets.py` (límites de reintentos), `src/continuum/recovery/engine.py` + `src/continuum/recovery/contract.py` + `src/continuum/recovery/planner.py` + `src/continuum/recovery/observations.py` (severidad máxima `RESUME < ... < ABORT`, contrato sellado con `evidence` / `reason` / `next_allowed_action` / `human_steps`), `src/continuum/checkpoint/rewind.py` (rebobinado atómico de doble estado), `src/continuum/analysis/prefix_trust.py` (confianza consultiva). Comprobaciones publicadas: `docs/recovery_walkthrough.md`, `benchmarks/fault_injection/` (suite que imprime `detection_rate` / `unsafe_resume_rate`), `src/continuum/benchmark/phase6/` (suite de corrección de recuperación), `docs/RESULTS.md` y el visual regenerable de abajo. |

Cada fila de arriba es rastreable a una ruta que existe en `main` en el commit etiquetado. Nada en esta tabla vuelve a exponer un número de benchmark, los benchmarks solo viven en la salida de la suite que ya los imprime. Consulta `docs/research.md` para la lista completa de suites publicadas y documentos de diseño.

### Recuperación tras caída, de verdad

La imagen de abajo no es una maqueta. Es la salida de `python demo-run/generate_crash_visual.py`, que ejecuta `demo-run/worker.py` hasta `os._exit(9)` en el documento 399, llama a `continuum resume --env dataset=v4` y muestra la ruta de rechazo (`REQUEST_HUMAN`, `safe:false`, exit 20), reconcilia el efecto secundario incierto con una sonda, luego se reanuda desde la misma base de datos y termina sin trabajo duplicado. La transcripción también se guarda como `docs/assets/crash-recovery.txt` para auditoría.

Regenerarlo:

```bash
python demo-run/generate_crash_visual.py
# o: python scripts/generate_crash_visual.py
```

![Recuperación tras caída: muerte brusca a mitad de lote, rechazo, reconciliación, reanudación](docs/assets/crash-recovery.svg)

Recorrido completo con código en `docs/recovery_walkthrough.md` (`examples/recovery_walkthrough.py`). El harness mínimo de bench está en `references/bench.md` (`continuum benchmark`).

## Características

| Capacidad | Qué te aporta |
|:--|:--|
| Checkpoints semánticos | Estado compacto, versionado e inspeccionable, no un volcado de transcripción |
| Libro mayor idempotente | Rechaza efectos secundarios externos duplicados, expone los inciertos para reconciliación |
| Revalidación del entorno | Cada componente del checkpoint se verifica contra el mundo actual antes de reanudar |
| Estado con procedencia | El progreso reportado por el agente se marca `REQUIRES_REVIEW`, nunca se auto certifica |
| Motor de recuperación | Siete modos de recuperación con un contrato determinista y sellado para la siguiente acción |
| Servidor MCP que deniega por defecto | Once herramientas, separación lectura/mutación, allowlist de llamantes |
| Adaptadores de frameworks | Integraciones Python genérico, OpenAI Agents SDK, LangGraph y LangChain |
| Bucle de planificación seguro | La verificación de observaciones con dos señales escala ramas de alto riesgo a REQUIRES_REVIEW |
| Revalidación periódica | El entorno se vuelve a comprobar según agenda, detectando deriva a mitad de ejecución dentro de un ciclo |
| Registro a prueba de manipulaciones | Registro de eventos encadenado (36 tipos de eventos) con verificación de integridad |
| Puerta de cumplimiento | Llamadas a efectos secundarios no reclamadas se rechazan antes de ejecutarse, los mensajes de denegación enseñan el protocolo de reclamo |
| Hooks de observación | Cada archivo que una CLI de código escribe se convierte en evidencia verificada por digest, fuera del control del modelo |
| Briefing de sesión | Sesiones frescas aprenden el estado de la ejecución de forma determinista al inicio, incluido el resumen de razonamiento de la sesión anterior |
| Sondas reconciliadoras | Comandos registrados liquidan efectos secundarios inciertos automáticamente, los humanos solo ven el resto |
| Guía ejecutable | Resume y validate renderizan los siguientes pasos como comandos ejecutables, no como estados |
| Gateway HTTP de cumplimiento | Llamadas salientes en cualquier lenguaje requieren reclamos, las respuestas se liquidan desde el código de estado real |
| Puente OpenTelemetry | Los spans de llamadas a herramientas del tracing de producción se convierten en evidencia sin cambios de código |
| Índice de acciones | Las búsquedas de idempotencia entre ejecuciones son lecturas indexadas, no escaneos completos |
| Fijación de versiones | Hashes de prompt, herramienta y modelo afirmados por el llamante se almacenan por reclamo, la deriva aflora al reanudar |
| Presupuestos de reintentos | Límites de intentos por tipo de acción impuestos al reclamar, los agentes ven los intentos restantes |
| Padre/hijo multiagente | La reanudación del padre compone el peor estado de la familia, el hijo incierto bloquea al padre |
| Reintento informado | Resúmenes de fallo redactados por el motor se inyectan en reanudaciones posteriores a la recuperación |
| Semántica de bifurcación | Continuaciones divergentes se ramifican en ejecuciones hijas con autoridad fresca |
| Compactación de registro | El prefijo pre-ancla se archiva verbatim, el registro vivo permanece acotado para ejecuciones de meses |
| Seguimiento de concesiones consumidas | Referencias de autoridad de un solo uso se marcan como gastadas en estado terminal, la reutilización tras restaurar se rechaza (`GRANT_DENIED`), defendiendo la ruta de restauración contra resurrección de autoridad |
| Atestación de cadena | `continuum attest` firma la cabeza de cadena de una ejecución con Ed25519 para que un verificador externo pueda probar que el historial no fue alterado con una clave conocida |
| Superficie HITL del dashboard | Botones de confirmar, reconciliar y completar con paridad de auditoría respecto a la CLI |

## Extensión de seguridad

Dos extensiones de seguridad aditivas se asientan sobre el sustrato de recuperación y checkpoint. No cambian la reanudación, la reproducción ni la ruta existente de revalidación en el momento de la caída.

- **Bucle de planificación seguro**: las observaciones llevan procedencia y se verifican con dos señales independientes (`verified` / `unverified` / `contested`). Una rama del plan protegida por una observación no verificada o disputada se escala a `REQUIRES_REVIEW`. Las decisiones se añaden al libro mayor como eventos `PERCEPTION_OBSERVED` y `BRANCH_RESOLVED`.
- **Revalidación periódica**: reutiliza el motor de recuperación en un intervalo de pasos (por defecto 25) y al cambiar de aplicación, por lo que la deriva del entorno a mitad de ejecución se detecta dentro de un ciclo en lugar de solo en la próxima caída.

Consulta [docs/PROBLEM.md](docs/PROBLEM.md), [docs/RESULTS.md](docs/RESULTS.md) y [STATUS.md](STATUS.md).

## Verificación empírica

CONTINUUM se verifica contra agentes LLM reales, límites de protocolo en vivo y caídas duras de proceso, no solo tests unitarios con mocks.

- **Agentes reales**: lotes de facturas multi sesión con Claude Code con `SIGKILL` a mitad de ejecución, puntuados 7/7 en mecánicas, las sesiones reanudadas consultaron `continuum_resume`, enrutaron efectos secundarios por el libro mayor en dos fases, se negaron a duplicar escrituras verificadas y respetaron `request_human`. Las pruebas en vivo expusieron huecos de deduplicación por deriva de prompt, cerrados con normalización de ruta canónica y respaldo basado en tokens en `ActionLedger.claim()`.
- **Clientes de terceros**: Gemini CLI y Kilo Code conectados vía stdio JSON-RPC contra el almacén SQLite en vivo, validando coexistencia multiagente y aislamiento de autorización.
- **Cumplimiento de protocolo**: conducido de extremo a extremo con `@modelcontextprotocol/inspector --cli` a través de muertes de proceso, las herramientas mutantes deniegan por defecto tras `CONTINUUM_MCP_MUTATING_CLIENTS`, los reclamos externos degradan a `REQUIRES_REVIEW` (`safe: false`).
- **Auto reparación**: servidores matados de forma brusca se recuperan de sidecars huérfanos `-wal`/`-shm` de SQLite mediante limpieza de un solo reintento al arrancar.
- **Escala**: cerca de 1,380 tests recogidos (~1,360 pasando, el resto se salta sin servicios opcionales) en Python 3.11, 3.12 y 3.13 (unitarios, basados en propiedades con `hypothesis`, concurrencia, adversariales). CONTINUUM-Bench ejecuta cinco escenarios de caída más un escenario dedicado de deriva de argumentos, midiendo 0 trabajo duplicado y 0 efectos secundarios duplicados para CONTINUUM frente a duplicación total para la reproducción ingenua, más una suite separada de 12 escenarios de corrección de recuperación (`continuum.benchmark.phase6`) que codifica los puntos de caída del estudio de ejecución durable como aserciones ejecutables.
- **Auditoría adversarial**: la superficie MCP completa fue auditada sobre el protocolo en vivo, se encontraron y corrigieron tres defectos. Método y pasos de reproducción en [test.md](test.md).

## Integración MCP

CONTINUUM entrega un servidor MCP para que un agente pueda registrar progreso, hacer checkpoint y enrutar efectos secundarios externos por el libro mayor sin embeber la librería:

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

Once herramientas vía stdio. Tres son de solo lectura (`continuum_validate`, `continuum_resume`, `continuum_list_actions`), ocho mutan. Los efectos secundarios son en dos fases (reclamar, ejecutar, completar) y las herramientas mutantes deniegan por defecto tras una allowlist. El estado reportado por el agente se registra con procedencia `Origin.EXTERNAL_AGENT` y se marca `REQUIRES_REVIEW`.

Detalles de verificación, incluida la recuperación tras caída al arrancar y la prueba extremo a extremo con Claude Code, en [references/mcp.md](references/mcp.md). Si un servidor registrado reporta `CONNECTION_CLOSED`, la causa casi siempre es la resolución de `PATH` y no el servidor en sí: [docs/api/mcp.md](docs/api/mcp.md#troubleshooting) tiene el diagnóstico y dos remedios.

## Integración de frameworks

Nueve adaptadores se entregan en `src/continuum/adapters/` (una fachada en proceso más ocho integraciones), todos instalables de forma opcional para que el núcleo siga siendo solo de la librería estándar:

| Adaptador | Clase | Notas |
|:--|:--|:--|
| Agente Python genérico | `GenericAgentAdapter` | Fachada en proceso, escribe estado confiable (`Origin.DETERMINISTIC`). |
| Sandbox de sistema de archivos | `FilesystemSandboxAdapter` | Sandbox de directorio local, sin servicio externo, valor por defecto para docs y CI. |
| Python en proceso | `PythonInProcAdapter` | Ejecuta Python en un directorio de trabajo temporal, registra vía libro mayor. |
| Contenedor | `ContainerAdapter` | Respaldado por Docker, salto protegido cuando `docker` falta. |
| Navegador | `BrowserAdapter` | Respaldado por Playwright, salto protegido cuando no está instalado. |
| Kubernetes | `KubernetesAdapter` | Respaldado por `kubectl`, salto protegido cuando no está configurado. |
| OpenAI Agents SDK | `OpenAIAgentAdapter` | Experimental. Engancha `ToolContext` / `RunHooks`, opcional `openai-agents`. |
| LangGraph | `LangGraphAgentAdapter` | Experimental. Envuelve un `StateGraph`, opcional `langgraph`. |
| LangChain | `LangChainAgentAdapter` | Experimental. Deja `checkpoint_node` en un pipeline `Runnable` de LCEL y en el bucle de llamada a herramientas de `create_agent`, opcional `langchain`. |

Cada adaptador registra progreso vía el libro mayor y enruta efectos externos por el protocolo de dos fases de intercepción y completado. Los tres adaptadores de framework tienen tests de integración extremo a extremo y han sido conducidos contra un **modelo vivo de OpenRouter**, donde las ejecuciones expusieron y cerraron una brecha de deduplicación por deriva de argumentos de LLM y dos defectos del adaptador de OpenAI, incluido un hard crash vivo (`os._exit(137)` en mitad de efecto secundario) por adaptador. Uso completo, resultados con modelo vivo y ejemplos ejecutables para cada adaptador en [references/adapters.md](references/adapters.md).

Las apps de producción con LangGraph también pueden mantener su API de persistencia nativa: `make_continuum_checkpointer(storage)` implementa `BaseCheckpointSaver` de LangGraph sobre el almacenamiento de CONTINUUM, por lo que cada put aterriza en el mismo registro de eventos encadenado y con procedencia (ver [references/adapters.md](references/adapters.md)).

Otras tres frameworks de producción están cubiertos por superficies delgadas de hooks sin SDK en [`adapters/thin.py`](src/continuum/adapters/thin.py):

| Framework | Superficie de intercepción | Punto de entrada |
|:--|:--|:--|
| CrewAI | hooks globales antes/después de llamada a herramienta | `install_crewai_hooks(storage, run_id)` |
| AutoGen core | `FunctionTool.run_json` envuelto en el sitio | `wrap_autogen_tool(tool, storage, run_id)` |
| Pydantic AI | capacidad asíncrona de Hooks | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` |

Para stacks que ninguno de estos alcanza: `continuum gateway` impone reclamos en HTTP saliente desde cualquier lenguaje, `continuum.otel.make_span_processor(storage)` convierte spans existentes de OpenTelemetry de herramientas en evidencia, y `continuum serve` expone las mismas operaciones que las herramientas MCP sobre un protocolo de cable JSON agnóstico al lenguaje (stdio, o HTTP vía `--transport http` con autenticación `CONTINUUM_SERVE_TOKEN`).

### Reanudando ejecuciones reportadas por agente o MCP

El estado reportado vía MCP, o a través del adaptador de OpenAI, lleva procedencia `Origin.EXTERNAL_AGENT` y se resuelve a `request_human` hasta confirmarse. Las ejecuciones de LangGraph y LangChain usan `Origin.DETERMINISTIC` y se reanudan directamente. Para limpiar la revisión y reanudar:

```bash
continuum confirm <run_id>   # registra REVIEW_CONFIRMED, luego reevalúa
continuum resume <run_id>    # ahora reporta RESUME
```

Sobre MCP el equivalente es la herramienta `continuum_confirm` seguida de `continuum_resume`. La confirmación es un evento único y atestiguado por humano: la escotilla de escape para la seguridad de auto certificación, por lo que una ejecución dirigida externamente nunca queda atascada de forma permanente.

## Conceptos clave

La referencia profunda para cada concepto vive en [references/concepts.md](references/concepts.md).

- **Checkpoints semánticos**, una representación compacta y versionada de lo que el agente necesita para continuar.
- **Validación de estado**, cada componente verificado de forma independiente, la obsolescencia se propaga por el grafo de dependencias.
- **Libro mayor idempotente**, los efectos secundarios externos se rastrean y deduplican, los resultados inciertos lanzan en lugar de reintentar silenciosamente.
- **Modos de recuperación**, `RESUME`, `REPAIR_AND_RESUME`, `ROLLBACK`, `WAIT`, `REQUEST_HUMAN`, `ABORT` (más `REPLAN`).
- **Contrato de recuperación**, una siguiente acción determinista, sellada por integridad y protegida.

## Arquitectura

CONTINUUM se organiza en torno a un invariante: **cada hecho lleva su origen, y la confianza se gana, nunca se asume.** Por qué importa para una startup: un agente que corre durante semanas no debe perder trabajo cuando su contexto se pierde, y no debe malgastar tokens, coste o disparar una herramienta dos veces.

### Sistema de un vistazo, adaptador universal, un registro, cualquier harness

Cualquier harness se conecta al mismo registro encadenado. La misma ejecución puede ser escrita por Claude Code, reanudada por LangGraph, inspeccionada por la CLI y aprobada en el dashboard. No se requiere cooperación del framework.

```text
  Claude Code ─┐
  Gemini CLI ──┤
  Codex ───────┤
  LangGraph ───┼── 5 costuras ──►  Un registro durable  ──►  Recuperación + Dashboard + CLI
  LangChain ───┤                (encadenado,        (contrato sellado,
  OpenAI SDK ──┤                 con procedencia,     verificación, salud,
  CrewAI ──────┤                 exactamente una vez)  familia)
  Cualquier HTTP ──┤
  Cualquier app OTel ┘

  Costuras: 1 En proceso  2 MCP  3 Hooks CLI  4 Gateway  5 OTel
```

### Las tres garantías (la demo prueba cada una)

1. **Sin auto certificación.** El estado reportado por el agente es `EXTERNAL_AGENT` y degrada a `REQUIRES_REVIEW` hasta un `REVIEW_CONFIRMED` humano. Solo escritores confiables producen estado `DETERMINISTIC`.
2. **Los efectos secundarios requieren reclamos.** Cada efecto externo se reclama en un libro mayor idempotente antes de dispararse. Los efectos no reclamados se bloquean en el límite, los duplicados se rechazan, los resultados inciertos se elevan para reconciliación.
3. **La recuperación verifica contra la realidad.** La reanudación comprueba digests de archivos, versiones de dependencias e identidad del modelo antes de decir que es seguro. La obsolescencia se propaga `dependency -> evidence -> finding -> decision` más `PlanStep.depends_on` por lo que solo los pasos afectados se reparan.

### Cinco costuras de integración

| Costura | Cómo conectar | Qué te aporta |
|:--|:--|:--|
| 1 En proceso | `GenericAgentAdapter.intercept_action(...)` y `wrap_tool(key_fn=...)` en LangChain, LangGraph, OpenAI Agents SDK | Frameworks Python, escrituras confiables |
| 2 Servidor MCP | `continuum-mcp` 12 herramientas vía stdio (`continuum_record_progress`, `continuum_intercept_action`, `continuum_complete_action`, etc.) | Cualquier cliente capaz de MCP, 3 solo lectura + 8 mutantes, allowlist `CONTINUUM_MCP_MUTATING_CLIENTS` |
| 3 Hooks de ciclo de vida CLI | `continuum hooks install claude-code --with-gate` también `gemini` y `codex` | CLIs de código: `SessionStart briefing`, `PostToolUse observe`, `PreToolUse gate`, sin necesidad de CLAUDE.md |
| 4 Gateway HTTP de cumplimiento | `continuum gateway --port 8765` con `.continuum/gateway.json` | Cualquier lenguaje, cualquier HTTP saliente debe tener un reclamo, el gateway liquida desde el código de estado real |
| 5 Puente OpenTelemetry | `make_span_processor(storage)` | Cualquier app trazada, los spans se convierten en evidencia `TOOL_COMPLETED` |

Superficies delgadas de hooks para CrewAI, AutoGen, Pydantic AI viven en `adapters/thin.py` sin necesidad de SDK.

### Pipeline de cumplimiento, por qué sin duplicados y sin llamadas inválidas

El pipeline de puerta a observación cierra la brecha en el límite del harness. Esto es lo que ahorra tokens y coste y bloquea llamadas inválidas a herramientas.

```text
Hook PreToolUse                    Hook PostToolUse
    |                                    |
    v                                    v
continuum gate                    continuum observe
    |                                    |
    |-- sin reclamo? DENIEGA (exit 2)          |-- evento TOOL_COMPLETED:
    |   + instrucciones para reclamar          |     ruta, bytes, sha256 en disco ahora
    |                                    |
    |-- reclamo vivo? PERMITE                |-- estado verificado en disco:
    |                                    |     verificado / cambiado / faltante
    v
el agente ejecuta el efecto
    |
    v
continuum_complete_action  (liquidado desde la realidad, no desde el reporte)
    |
    v
libro mayor marcado COMPLETADO, la próxima reproducción devuelve resultado cacheado, no un segundo disparo
```

Host desconocido se deniega cerrado por fallo, no como relay abierto. Shell `Bash/curl` es el punto ciego documentado de v1.

### Árbol de decisión de recuperación, semanas hasta terminar, correcto y exacto

El motor toma la señal más cautelosa, por lo que la seguridad nunca pierde ante la conveniencia.

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

Cada `continuum resume` devuelve un contrato sellado con: estado de recuperación y `safe`, componentes verificados e invalidados, `human_steps` ejecutables (shell exacto a ejecutar), observaciones tras checkpoint verificadas en disco, deriva de fijación y agregación familiar para `continuum tree` multiagente. El briefing `continuum briefing` inyecta ese contrato en cada `claude` SessionStart fresco, por lo que decir `hola` después de matar la terminal se reanuda desde el último prefijo bueno.

### Por qué esto ahorra tokens, coste y llamadas inválidas

* **Tokens:** El checkpoint semántico almacena `Goal + Plan + Progress` no un volcado de transcripción. El briefing sirve solo estado verificado más un resumen de razonamiento con tope de 4096, no la cola de errores que el auto condicionamiento muestra que degrada la siguiente sesión. El reintento informado `recovery/summary.py` inyecta un resumen redactado por el motor, no historial crudo.
* **Coste:** El libro mayor `action_index` rechaza efectos secundarios duplicados incluso bajo deriva de argumentos como rutas relativas frente a absolutas (`invoice:INV-001` clave estable), por lo que la misma API no se paga dos veces tras una reanudación. Los presupuestos `budgets.py` limitan tormentas de reintentos al reclamar. `continuum benchmark` imprime `0 duplicados` para continuum frente a `50` para el ingenuo.
* **Llamadas inválidas:** La puerta, el gateway y `replayguard` con `langgraph_protected_node` bloquean llamadas a herramientas no reclamadas o reproducidas antes de dispararse. La fijación `pinning.py` expone deriva de prompt o herramienta al reanudar.

### Arquitectura de almacenamiento

Esquema v6. SQLite es primario, Postgres verificado por CI. Un registro, muchas proyecciones.

| Tabla | Propósito |
|:--|:--|
| `events` | Registro solo anexado encadenado (44 tipos de eventos en v0.2) |
| `runs` | Metadatos de ejecución con `parent_run_id` para multiagente |
| `versions` | Instantáneas de SemanticState por checkpoint |
| `checkpoints` | Registros de checkpoint sellados con anclas `RECOVERY` |
| `action_index` | Proyección de idempotencia entre ejecuciones (schema v3+), lecturas indexadas, no escaneos completos |
| `events_archive` | Almacenamiento de prefijo compactado (schema v5+), `continuum compact` acota el registro vivo para ejecuciones de meses |
| `lg_checkpoints` / `lg_writes` | Persistencia nativa de LangGraph (schema v4+), `make_continuum_checkpointer(storage)` |

### Mapa de módulos, una librería, muchas superficies

CONTINUUM es una librería (`src/continuum`, 104 módulos) más una suite de tests grande (98 archivos de test, ~1,380 tests). Todos los módulos añaden y reproducen un registro de eventos encadenado:

| Módulo | Rol |
|:--|:--|
| `events.py` | Registro solo anexado encadenado y `verify() trusted_through` |
| `state/` | Proyección `project()`, validación, extracción, propagación de obsolescencia |
| `storage/` | `SQLiteStorage` v6, `postgres.py`, `migrations.py`, `actionindex.py` |
| `actions/` | Libro mayor idempotente `claim/complete/reconcile`, `idempotency.py` clave y canonización y respaldo por token, seguimiento de concesión consumida `GRANT_DENIED` |
| `checkpoint/` | Checkpoints dirigidos por políticas `manager.py` `policy.py` con anclas `RECOVERY` y `prune` |
| `recovery/` | Motor, planificador, contrato sellado `contract.py`, `guidance` `human_steps`, `observations` verificadas en disco, `family` rollup, `fork` semántica, `summary` reintento informado |
| `gate.py` | Cumplimiento antes de herramienta: permitir o denegar contra reclamos del libro mayor |
| `gateway.py` | Proxy HTTP de cumplimiento: reclamar antes de disparar para solicitudes salientes |
| `replayguard.py` | Guardia portable: `evaluate, protected_call, langgraph_protected_node`, cierra riesgo de reproducción de ACRFence |
| `hooks.py` `clienthooks.py` | Hooks de checkpoint compartidos y perfiles de instalador `claude-code gemini codex` |
| `budgets.py` | Registro y evaluación de presupuesto de reintentos por tipo de acción |
| `pinning.py` | Normalización de fijación de versiones y detección de deriva al reanudar |
| `replay_similarity.py` | Backends de similitud semántica exact/fuzzy/embedding para reproducción vs bifurcación |
| `reconcilers.py` | Registro de sondas `.continuum/reconcilers.json` para liquidación automática |
| `adapters/` | 9 adaptadores de clase + hooks delgados `thin.py` CrewAI AutoGen Pydantic AI + almacén LangGraph |
| `mcp/` | 12 herramientas stdio más autorización `authz.py` autenticación por token, allowlist, token de confirmación |
| `serve/` | Sidecar stdio cable JSON + HTTP `CONTINUUM_SERVE_TOKEN` |
| `dashboard/` | Dashboard web `app.py` `hitl.py` con botones HITL confirmar/reconciliar/completar, aviso de confianza de prefijo, fijaciones |
| `cli/` | 38 comandos argparse, códigos de salida como veredicto, `runs, start, inspect, resume, verify, health, tree, benchmark, attest, dashboard` |
| `otel.py` | Puente de procesador de spans de OpenTelemetry |
| `benchmark/` | Harness de CONTINUUM-Bench, 5 escenarios de caída + deriva de argumentos + suite de recuperación de 12 escenarios |

### Limitaciones honestas

- La puerta no ve dentro de comandos shell (Bash/curl elude reclamos de herramientas estructuradas)
- El backend de Postgres está probado por CI pero no curtido en producción
- Aún no hay webhook saliente para notificaciones `request_human` (#305)
- Un nivel de jerarquía multiagente en v1
- Descarga de payloads grandes (#254) aún no implementada
- El benchmark a escala de semanas con tabla de coste de tokens aterriza en el tablero #550 (#568 a #570)

Referencia completa en [references/architecture.md](references/architecture.md). Y el plano de meses que se construye sobre esto, grafo causal de procedencia, resurrección de autoridad, admisibilidad, vivacidad, está fijado como tablero #550 con 20 sub issues #551 a #570.

## API y CLI

Superficie Python (`EventType`, `Run`, `SQLiteStorage`, `diff_states`, `project`) y la API de adaptadores están documentadas con ejemplos ejecutables en [references/api.md](references/api.md). La CLI es la misma superficie en forma de shell:

```bash
continuum runs                                   # listar ejecuciones
continuum inspect <run_id>                       # estado semántico
continuum validate <run_id> --env dataset=v4     # validar, solo lectura
continuum resume <run_id> --env dataset=v4       # decisión de recuperación + contrato + siguientes pasos
continuum checkpoint <run_id>                    # forzar un checkpoint, muta
continuum actions <run_id>                       # efectos secundarios externos
continuum reconcile <run_id>                     # liquidar efectos inciertos con sondas
continuum complete <run_id>                      # cerrar una ejecución como hecha, desde el teclado
continuum verify <run_id>                        # reauditar la cadena de hash de eventos
continuum budget <run_id>                        # uso de presupuesto de reintentos por tipo de acción
continuum compact <run_id>                       # archivar prefijo de registro pre-ancla
continuum tree <parent_run_id>                   # mostrar padre + hijos con estados de recuperación
continuum attest <run_id> --key signer.pem       # firmar la cabeza de cadena para un verificador externo
```

Todo el cableado está del lado del host, la cooperación del modelo es opcional:

```bash
continuum hooks install claude-code --with-gate   # CLIs de código: evidencia, briefing, puerta
continuum gateway --port 8765                     # proxy HTTP de cumplimiento para todo lo demás
provider.add_span_processor(continuum.otel.make_span_processor(storage))  # OTel a evidencia
continuum-mcp                                     # cualquier cosa capaz de MCP: el servidor de once herramientas
continuum briefing                                # inyección de contexto al inicio de sesión
continuum budget <run_id>                        # informe de uso de presupuesto de reintentos
continuum tree <parent_run_id>                   # vista de jerarquía multiagente
```

Los registros opcionales viven junto a tu código y son datos, no código: `.continuum/gate.json` (herramientas de efecto secundario + plantillas de clave estable), `.continuum/reconcilers.json` (sondas que comprueban sistemas externos), `.continuum/gateway.json` (rutas ascendentes).

Cada comando acepta `--json`, y los comandos de solo lectura nunca escriben, por lo que son seguros contra una base de datos viva mientras un agente está a mitad de ejecución. Los códigos de salida son un contrato de seguridad (solo una ejecución verificada segura sale con 0). Lista completa de comandos, tabla de códigos de salida y salida de diff de estado en [references/cli.md](references/cli.md).

## Hoja de ruta

| Fase | Componente | Estado |
|:--:|:--|:--|
| 1-11 | Modelos de datos, estado semántico, persistencia, checkpointing, validación, libro mayor de acciones, motor de recuperación, CLI, ejemplos de recuperación tras caída, instantáneas y diffs de entorno, adaptadores de framework | Completo |
| 12 | Suite de benchmarks (CONTINUUM-Bench) | Completo (harness mínimo) |
| 13 | API en la nube (FastAPI + PostgreSQL) | Parcial: el backend de almacenamiento PostgreSQL y el transporte sidecar HTTP (`continuum serve --transport http`) están entregados y probados por CI, el servicio multi-tenant hospedado no ha empezado |
| 14 | Dashboard | Completo (`continuum dashboard`) |
| 15+ | Durabilidad impuesta: hooks de observación, puerta, briefing de sesión, sondas reconciliadoras, gateway de cumplimiento, puente OTel, índice de acciones, guía ejecutable, instaladores multi cliente, detección de reproducción semántica, fijación de versiones, presupuestos de reintentos, compactación de registro, superficie HITL, semántica de bifurcación, reintento informado, agregación multiagente | Completo (ver issue #213) |
| Siguiente | Plano de durabilidad a escala de meses: planes anclados a hitos (#312), memoria de intentos estructurada (#313), rebobinado atómico de doble estado (#292), benchmark público de corrección de recuperación (#293), notificaciones salientes por webhook (#305) | Planificado (borrador de especificación en [docs/UPGRADE_SPEC.md](docs/UPGRADE_SPEC.md)) |

Más allá del plan original: el servidor MCP, las capas de autorización MCP y autenticación de llamante, procedencia y anti auto certificación, archivos de comunidad, versionado de esquema con migraciones hacia adelante, un contexto de recuperación acotado, seguimiento de concesiones consumidas, atestación de cadena de eventos Ed25519, el checkpointer nativo de LangGraph y artefactos wheel en cada push a `main` están entregados. Consulta [STATUS.md](STATUS.md) para el desglose verificado frente a creído y los defectos de corrección abiertos.

## Lo que CONTINUUM no es

| No es esto | En cambio es esto |
|:--|:--|
| Un LLM | Una capa de fiabilidad para agentes que usan LLMs |
| Un framework de agentes | Una capa de recuperación que se conecta a cualquier framework |
| Una base de datos vectorial | Estado semántico estructurado, no embeddings |
| Un sistema RAG | Checkpoints verificados, no memoria aumentada por recuperación |
| Un motor de flujo de trabajo | Una capa de recuperación, no un orquestador |

La abstracción central: `estado semántico + validación del entorno + reconciliación de acciones = recuperación segura`.

## Trabajo relacionado

CONTINUUM se sitúa en la intersección de ejecución durable, seguimiento idempotente de efectos secundarios y recuperación tras caída para agentes LLM. Los vecinos más cercanos son contratos de reanudación verificados por máquina (Khan 2026), procesamiento transaccional agéntico con admisión protegida por restricciones (Mnemosyne 2026), análisis de ataques de reversión de checkpoint (ACRFence 2026) y defensa de inyección de prompt a nivel de diseño (CaMeL 2025). La lista completa anotada, fundamentos y auditoría de citas están en [references/related-work.md](references/related-work.md).

## Estado y limitaciones

- **Probado**: 1,360 pasados + 23 saltados en una ejecución completa en la auditoría del 2026-08-24 de este árbol, CI hace cumplir la suite en Python 3.11, 3.12 y 3.13, y los conteos varían por plataforma y servicios opcionales como Postgres (ver [STATUS.md](STATUS.md)). La superficie MCP también ha sido auditada de forma adversarial sobre el protocolo en vivo, ver [test.md](test.md).
- **En PyPI como `continuum-agent` 0.1.0** (`pip install continuum-agent`, el clon aún funciona vía `pip install .` ver Inicio rápido).
- **La autenticación de llamante MCP es opcional por despliegue.** Cuando se establece `CONTINUUM_MCP_TOKEN`, el servidor rechaza cada herramienta mutante a menos que el llamante presente ese secreto compartido en el `_meta.authToken` del handshake `initialize`, secretos por llamante disponibles vía `CONTINUUM_MCP_CLIENT_TOKENS` (pares `name:secret`). Sin ningún token configurado, la autorización es solo por identidad declarada (el valor histórico por defecto, preservado para uso local de un solo usuario).
- **Confirmar estado auto reportado vía MCP requiere un secreto separado.** `continuum_confirm` rechaza a cada llamante hasta que el operador establece `CONTINUUM_MCP_CONFIRM_TOKEN`, porque un agente al que se le permite registrar progreso no debe poder confirmarlo también. La ruta por defecto sigue siendo conducida por humano: ejecuta `continuum confirm <run_id>` en el host.
- **Componentes no construidos**: API en la nube (Fase 13).
- **Brecha de cumplimiento en comandos shell**: la puerta impone reclamos para llamadas a herramientas estructuradas pero no puede ver dentro de comandos Bash o curl. Documentado como alcance v1 rechazado.
- **Los adaptadores de framework siguen siendo experimentales.** Los tres adaptadores de framework ahora llevan pruebas de reanudación suave y caída dura con modelo vivo (OpenRouter, `gpt-4o-mini`), incluida la prueba de contrato de caída que bloquea la reanudación sobre un efecto secundario incierto, y ahora tienen tests de verificación de caída y reanudación que alcanzan paridad con la fachada genérica (Refs #285). Prefiere `GenericAgentAdapter` para recuperación en producción.
- **Las ejecuciones de agente y MCP necesitan una confirmación explícita antes de la reanudación automática.** El estado reportado externamente es `REQUIRES_REVIEW`, por lo que `continuum resume` devuelve `request_human` hasta que un humano confirma. Por diseño, no es un defecto, ver [Integración de frameworks](#integración-de-frameworks).
- **Serie de tests de autonomía e2e** (issue [#6](https://github.com/Cyrax321/CONTINUUM/issues/6)): tres ejecuciones completas de Claude Code puntuaron 7/7 en mecánicas con comportamiento de recuperación no solicitado observado. Más iteraciones a través de diversos estilos de prompt permanecen abiertas.

## Sobre

A principios de 2026 vi agentes de larga duración fallar en la recuperación, no en el razonamiento. Los checkpoints se trataban como prueba para continuar, no como evidencia a verificar. Estudiando Temporal, LangGraph, ACRFence 2603.20625 y self conditioning 2509.09677, encontré que el hueco era un sustrato de verificación portable que pregunta, dado el estado en el tiempo T y el mundo tal como está ahora, sigue siendo seguro continuar.

En tres semanas construí CONTINUUM desde un invariante, cada hecho lleva su origen. El resultado es un registro encadenado con `verify()`, un libro mayor con deduplicación por clave estable, una puerta y un gateway que bloquean efectos no reclamados, y un motor de recuperación que sella un contrato. Cinco costuras exponen el mismo registro a Claude Code, LangGraph, LangChain, OpenAI, HTTP y OpenTelemetry. Validado con muertes reales y 1380 tests, imprime `0 duplicados` donde la reproducción ingenua imprime `50`.

CONTINUUM fue creado por **Anandhu P Shaji** ([@Cyrax321](https://github.com/Cyrax321) · [LinkedIn](https://www.linkedin.com/in/anandhupshaji/)) y es mantenido por el creador original. Es de código abierto bajo [Apache-2.0](LICENSE). Las contribuciones de la comunidad son bienvenidas vía [CONTRIBUTING.md](CONTRIBUTING.md) y se acreditan en [AUTHORS.md](AUTHORS.md) y [graphs/contributors](https://github.com/Cyrax321/CONTINUUM/graphs/contributors).

## Contribuir

Este proyecto es de código abierto bajo Apache 2.0 y está deliberadamente construido para ser extendido: por investigadores que validan la semántica de recuperación, por ingenieros que portan el libro mayor o el servidor MCP a otros frameworks o lenguajes, y por cualquiera que convierta la hoja de ruta planificada en realidad. Un buen lugar para empezar es la etiqueta `good first issue` en el [rastreador de issues](https://github.com/Cyrax321/CONTINUUM/issues), o los defectos abiertos de corrección listados en STATUS.md.

Abre un issue antes de enviar PRs grandes. Consulta [CONTRIBUTING.md](CONTRIBUTING.md) para la guía completa de contribución, incluido el [Code of Conduct](CODE_OF_CONDUCT.md).

### Colaboradores

<a href="https://github.com/Cyrax321/CONTINUUM/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Cyrax321/CONTINUUM" />
</a>

## Patrocinar

Si CONTINUUM ayuda a tus agentes a recuperarse de forma fiable, considera patrocinar para apoyar el mantenimiento a largo plazo.

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/Sponsor-❤-ff69b4?style=for-the-badge&logo=githubsponsors" alt="Sponsor Cyrax321" /></a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321">Conviértete en patrocinador</a>, GitHub Sponsors, o añade un enlace personalizado en FUNDING.yml si prefieres otra plataforma.
</p>

## Licencia

Apache 2.0, ver [LICENSE](LICENSE).

---

Material de referencia profundo:

- [references/install.md](references/install.md) - requisitos previos, niveles de instalación, mapa de paquetes, verificación
- [references/concepts.md](references/concepts.md) - checkpoints semánticos, validación, libro mayor, modos de recuperación, contrato
- [references/architecture.md](references/architecture.md) - modelo de datos, registro de eventos, proyección, almacenamiento, checkpointing, motor de recuperación, seguridad
- [references/adapters.md](references/adapters.md) - uso de adaptadores de framework y resultados de validación con modelo vivo
- [references/api.md](references/api.md) - API de Python y adaptadores
- [references/cli.md](references/cli.md) - lista completa de comandos CLI, códigos de salida, diff de estado
- [references/mcp.md](references/mcp.md) - estado del servidor MCP, verificación, preguntas abiertas
- [references/bench.md](references/bench.md) - diseño de CONTINUUM-Bench
- [references/quickstart.md](references/quickstart.md) - instalación, ejemplos, los scripts de prueba
- [references/e2e.md](references/e2e.md) - recorrido de test de autonomía extremo a extremo
- [references/testing.md](references/testing.md) - disposición y convenciones de la suite de tests
- [references/related-work.md](references/related-work.md) - trabajo relacionado anotado y auditoría de citas
