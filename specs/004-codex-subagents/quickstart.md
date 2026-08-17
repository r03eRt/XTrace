# Quickstart: Validar configuración de subagentes Codex

## Prerequisites

- Codex con acceso a GPT-5.6 Luna y razonamiento Max.
- Python 3.11 o posterior para disponer de `tomllib`.
- Repositorio abierto en `chore/000-codex-subagents`.

## Static validation

Desde la raíz del repositorio:

```bash
python3 -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path(".codex/config.toml").read_text()); a=p["agents"]; assert a=={"enabled": True,"max_concurrent_threads_per_session": 3,"default_subagent_model": "gpt-5.6-luna","default_subagent_reasoning_effort": "max"}; assert "model" not in p and "model_reasoning_effort" not in p'
```

Resultado esperado: exit code `0` sin salida.

Después:

```bash
pnpm exec prettier --check specs/004-codex-subagents .specify/feature.json
git diff --check
```

## Functional smoke test

1. Reiniciar Codex o abrir una sesión nueva para XTrace.
2. Solicitar un subagente sin indicar modelo ni razonamiento.
3. Confirmar que el lanzamiento usa `gpt-5.6-luna` con esfuerzo `max`.
4. Confirmar que el agente principal conserva su modelo y razonamiento de sesión.

## Expected outcome

- La configuración es TOML válido.
- Los cuatro valores de subagente coinciden exactamente con la spec.
- No existen defaults de modelo o razonamiento del agente principal.
- No se modifican componentes de producto de XTrace.
