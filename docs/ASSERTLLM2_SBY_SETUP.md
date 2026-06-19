# AssertLLM2-SBY Setup

This stage uses the read-only official checkout at `third_party/AssertLLM2` and
keeps backend code outside that checkout.

Default backend config lives at `AssertLLM2/configs/assertllm2_sby.yaml`. It intentionally
contains no secrets.

## Python Environment

Use the dedicated Python 3.13 environment from the `assertllm2-sby/` directory:

```bash
source .venv-assertllm2-sby/bin/activate
python --version
python -m pytest tests -q
python scripts/check_assertllm2_sby_environment.py
python -m assertllm2_sby formal-self-test
```

The environment is created from:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv-assertllm2-sby
```

Install missing Python dependencies only after activating this environment. Do
not install AssertLLM2-SBY test dependencies into the global Python.

## Generation Runtime

For a real generation call, the current generator uses the Anthropic client
settings exposed by this repository:

```bash
source .venv-assertllm2-sby/bin/activate
export ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1
export ANTHROPIC_API_KEY=...
export ASSERTLLM2_SBY_LLM_MODEL=claude-sonnet-4-6
export ASSERTLLM2_SBY_LLM_TEMPERATURE=0.0
export ASSERTLLM2_SBY_LLM_MAX_TOKENS=4096
python -m assertllm2_sby generate \
  --mode bug-prevention \
  --design <assertllm2/design/key>
```

To run an experiment with a different Anthropic model, set `ASSERTLLM2_SBY_LLM_MODEL` in
the shell or repository-root `.env` before invoking the CLI. This creates a
fresh run directory and records the effective non-secret model configuration in
the run artifacts. Do not change source defaults for a one-off experiment.

If credentials or model access are unavailable, `generate` fails closed and
writes a blocked generation manifest instead of fabricating assertions.

`ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1` is the explicit opt-in for Anthropic calls. Missing,
empty, or `0` values block cloud-model calls. The AssertLLM2-SBY CLI loads the
repository-root `.env` once with `python-dotenv` and `override=False`;
`ANTHROPIC_API_KEY` is then read from the process environment by the bundled
client. The backend must not print API-key values.

Anthropic performs assertion generation only. SymbiYosys/Yosys/Z3 perform formal
evaluation. The current formal backend is enabled after passing the synthetic
self-test.

See `docs/ASSERTLLM2_SBY_RUNTIME_CONFIG.md` for the exact non-secret runtime
configuration.
