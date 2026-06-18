# AssertLLM2-SBY

AssertLLM2-SBY is an open-source formal evaluation backend for
[AssertLLM2](https://github.com/hkust-zhiyao/AssertLLM2)-style benchmark
runs. It replaces the official JasperGold judge with a Yosys +
SymbiYosys + Z3 flow for assertion checking, coverage approximation, and
mutation scoring.

This project is intentionally honest about provenance:

- results are backend-specific and are not official JasperGold results
- results are not directly comparable to published JasperGold-based AssertLLM2 scores
- unsupported syntax, timeouts, and infrastructure failures are reported as such
- secrets are never written into manifests, prompts, reports, or logs

## What This Repo Contains

| Path | Purpose |
| --- | --- |
| `AssertLLM2/assertllm2_sby/` | Python package with CLI, generation, formal backend, mutation runner, and report writers |
| `AssertLLM2/configs/` | Default runtime configuration, with no secrets |
| `scripts/` | Environment preflight and validation helpers |
| `tests/` | Unit and integration tests |
| `docs/` | Setup, runtime configuration, and project rules |
| `vendor/llm_client.py` | Vendored Anthropic client used by the current generation path |
| `third_party/AssertLLM2/` | Read-only upstream benchmark checkout used as a git submodule |

## Requirements

- Python 3.13
- `yosys`
- `sby` / SymbiYosys
- `z3`
- `click`
- `python-dotenv`
- `requests`

The codebase supports Python 3.10+ in packaging metadata, but the current
checked-in environment and preflight are built around Python 3.13.

## Quick Start

From the repository root:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv-assertllm2-sby
source .venv-assertllm2-sby/bin/activate
python -m pip install -e ".[dev]"

git submodule update --init --recursive third_party/AssertLLM2

python scripts/check_assertllm2_sby_environment.py
python -m assertllm2_sby formal-self-test
python -m pytest tests -q
```

If you are publishing this repository on its own, keep `third_party/AssertLLM2`
as a submodule or point `--checkout` at a valid local AssertLLM2 checkout.

## CLI

The package exposes a single entry point:

```bash
python -m assertllm2_sby --help
```

Available commands:

- `list-designs`
- `capability-matrix`
- `inspect-design`
- `env-status`
- `formal-self-test`
- `prepare-input`
- `generate`
- `run-design`
- `run-suite`

Examples:

```bash
python -m assertllm2_sby list-designs --json
python -m assertllm2_sby capability-matrix --json
python -m assertllm2_sby inspect-design --design assertllm2/dsp_core/ima_adpcm_decoder
python -m assertllm2_sby formal-self-test
```

## Running A Design

### Spec-only generation path

This path uses the spec-only generator and keeps RTL hidden from the model.

```bash
export ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1
export ANTHROPIC_API_KEY=...

python -m assertllm2_sby run-design \
  --mode bug-prevention \
  --design assertllm2/dsp_core/ima_adpcm_decoder
```

### Contract-inference path

This path is meant for a local contract-inference engine that can read RTL.
It is the default product direction for this repository, but it is not
bug-prevention-compatible unless you deliberately hide RTL from the generator.

```bash
python -m assertllm2_sby run-design \
  --method contract-inference \
  --mode rtl-contract \
  --design assertllm2/dsp_core/ima_adpcm_decoder \
  --contract-python-entrypoint your_module:your_function
```

### Suite runs

```bash
python -m assertllm2_sby run-suite \
  --mode bug-prevention \
  --limit 5 \
  --jobs 2
```

Use `--resume <suite_dir>` to continue a prior suite directory.

## Environment Variables

### Cloud LLM generation

- `ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1` enables Anthropic calls
- `ANTHROPIC_API_KEY` must be present for the current generation path
- `ASSERTLLM2_SBY_LLM_MODEL` selects the model
- `ASSERTLLM2_SBY_LLM_TEMPERATURE` sets generation temperature
- `ASSERTLLM2_SBY_LLM_MAX_TOKENS` sets the token cap
- `ASSERTLLM2_SBY_LLM_TIMEOUT` sets the request timeout

### Contract-inference adapter

- `ASSERTLLM2_SBY_CONTRACT_PYTHON_ENTRYPOINT`
- `ASSERTLLM2_SBY_CONTRACT_EXECUTABLE`
- `ASSERTLLM2_SBY_CONTRACT_TOOL_ROOT`

Repository-root `.env` files are loaded with `override=False`, so they can
provide defaults without overwriting already-exported environment variables.

## Output Directories

- `results/<run_id>/` for one-design formal runs
- `runs/assertllm2-sby/` for generation workspaces
- `results/suites/<suite_id>/` for suite runs

Each run records:

- the effective mode and method
- the selected design key
- model or adapter metadata
- assertion artifacts
- compatibility reports
- mutation results
- provenance and environment snapshots

## Validation

The repo includes a fast preflight check and tests:

```bash
python scripts/check_assertllm2_sby_environment.py
python -m assertllm2_sby formal-self-test
python -m pytest tests -q
```

`formal-self-test` verifies the local formal toolchain and the current backend
without requiring cloud LLM access.

## Current Limits

This backend is useful now, but it is not a JasperGold clone.

- some AssertLLM2 designs still require parser and harness work
- multi-clock support is limited
- coverage reports are approximations with explicit non-Jasper provenance
- unsupported SVA is rejected or marked unsupported rather than silently weakened
- mutation kills require a real counterexample on a golden-accepted assertion

## Publishing Notes

When this repository is published publicly:

1. keep `third_party/AssertLLM2` as a read-only upstream checkout
2. keep secrets out of `.env`, configs, manifests, and committed logs
3. run `python scripts/check_assertllm2_sby_environment.py` before a release tag
4. include the `official_jaspergold_result: false` caveat in generated reports

## Further Reading

- [Setup guide](docs/ASSERTLLM2_SBY_SETUP.md)
- [Runtime config reference](docs/ASSERTLLM2_SBY_RUNTIME_CONFIG.md)
- [Project rules](docs/ASSERTLLM2_SBY_PROJECT_RULES.md)
