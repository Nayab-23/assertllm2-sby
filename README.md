# AssertLLM2-SBY

Open-source formal evaluation engine for [AssertLLM2](https://github.com/hkust-zhiyao/AssertLLM2)-style benchmark runs. This package replaces the official JasperGold judge with **Yosys + SymbiYosys + Z3** for assertion checking and mutation scoring.

Results from this engine are **backend-specific** and are not directly comparable to published JasperGold-based AssertLLM2 scores.

## What is in this folder

| Path | Purpose |
|------|---------|
| `assertllm2_sby/` | Python package: LLM generation, SVA lowering, SBY harness, mutation runner |
| `config/` | Default runtime YAML (no secrets) |
| `scripts/` | Environment preflight checker |
| `tests/` | Unit and integration tests |
| `docs/` | Setup, runtime config, project rules |
| `vendor/llm_client.py` | Vendored Anthropic HTTP client (no polaris-sable dependency) |
| `third_party/AssertLLM2/` | Read-only benchmark checkout (git submodule) |

## Quick start

From this directory (`assertllm2-sby/`):

```bash
python3 -m venv .venv-assertllm2-sby
source .venv-assertllm2-sby/bin/activate
pip install -e ".[dev]"

# If nested inside AssertNeuro, the parent checkout is used automatically.
# For a standalone clone:
git submodule update --init --recursive third_party/AssertLLM2

python scripts/check_assertllm2_sby_environment.py
python -m assertllm2_sby formal-self-test
python -m pytest tests -q
```

List benchmark designs:

```bash
python -m assertllm2_sby list-designs
```

Run one design end-to-end (requires cloud LLM opt-in):

```bash
export SABLE_ENABLE_CLOUD_LLM=1
export ANTHROPIC_API_KEY=...
python -m assertllm2_sby run-design \
  --mode bug-prevention \
  --design assertllm2/dsp_core/ima_adpcm_decoder
```

## External tools

- `yosys`
- `sby` (SymbiYosys)
- `z3`

## Outputs

- Formal run results: `results/<run_id>/`
- Generation workspaces: `runs/assertllm2-sby/`

## Extracting to a new repository

This folder is self-contained. To publish as its own repo:

1. Copy or move `assertllm2-sby/` out of AssertNeuro
2. `git submodule add https://github.com/hkust-zhiyao/AssertLLM2.git third_party/AssertLLM2`
3. Create a venv and `pip install -e ".[dev]"`

See `docs/ASSERTLLM2_SBY_SETUP.md` for full setup details.
