# AssertLLM2-SBY Runtime Configuration

Recorded on 2026-06-17 from the current adapter code, `config/assertllm2_sby.yaml`,
and `vendor/llm_client.py`.

## Python Runtime

- Canonical activation command: `source .venv-assertllm2-sby/bin/activate`
- Python environment path: `.venv-assertllm2-sby`
- Environment interpreter source: `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`
- Canonical test command: `python -m pytest tests -q`
- Environment checker: `python scripts/check_assertllm2_sby_environment.py` (from `assertllm2-sby/`)

## Assertion Generation

- Provider: Anthropic Messages API through `vendor/llm_client.py`
- Adapter transport: `assertllm2_sby/generator.py::_anthropic_transport`
- Default model ID: `claude-sonnet-4-6`
- Model override env var: `ASSERTLLM2_SBY_LLM_MODEL`
- Default temperature: `0.0`
- Temperature override env var: `ASSERTLLM2_SBY_LLM_TEMPERATURE`
- Default maximum output tokens: `4096`
- Max-token override env var: `ASSERTLLM2_SBY_LLM_MAX_TOKENS`
- Default API URL: `https://api.anthropic.com/v1/messages`
- API URL override env var: `ANTHROPIC_API_URL`
- Anthropic API version header: `2023-06-01`
- Default request timeout: `30` seconds
- Timeout override env var: `ASSERTLLM2_SBY_LLM_TIMEOUT`
- Thinking/reasoning configuration: none. The request body contains `model`,
  `max_tokens`, `temperature`, `system`, and `messages`; it does not set a
  thinking or reasoning field.
- Retry count: `0`. Neither the adapter transport nor `vendor/llm_client.py`
  implements a retry loop for this path.
- Generation attempts per design: `1` per adapter `generate` invocation.
- Optional call provenance log: `ASSERTLLM2_SBY_LLM_LOG`, written by `vendor/llm_client.py`
  when that module's logging helper is used. The AssertLLM2-SBY adapter writes
  its own generation artifacts under the selected output directory.

## Changing The Generation Model

Use environment or repository-root `.env` overrides. Do not hard-code model IDs
in source for an experiment.

Minimum non-secret override:

```bash
ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1
ASSERTLLM2_SBY_LLM_MODEL=<anthropic-model-id>
```

Keep `ANTHROPIC_API_KEY` in `.env` or the process environment only. Never copy
the key into manifests, configs, prompts, reports, shell history, or issue text.

Every model-change run must use a fresh result directory. The one-design runner
records the effective non-secret model configuration in:

- `model_configuration.json`
- `environment.json`
- `summary.json`
- `manifest.json`
- `report.md`

Recorded fields include provider, model ID, temperature, max tokens, timeout,
API version, prompt/template identifiers, attempts, retries, thinking mode,
Anthropic stop reason, input/output token counts when provided, and whether the
output appears truncated.
The API-key value, prefix, length, and hash are not recorded.

## Prompt Template

- System prompt location: `assertllm2_sby/generator.py::SPEC_ONLY_SYSTEM_PROMPT`
- User prompt builder: `assertllm2_sby/generator.py::_build_user_prompt`
- Prompt mode currently supported: `bug-prevention`
- Default spec source: `spec_md`
- Prompt input isolation: only copied specification files listed in the isolated
  workspace manifest are exposed. Golden RTL, support RTL, mutants, prior
  results, `.git`, and `.env` are excluded from the generation workspace.
- Per-run prompt artifacts: `model_system_prompt.txt` and `model_user_prompt.txt`
  in the generation output directory.

The official AssertLLM2 `plain_prompt` templates remain in the upstream checkout
under `third_party/AssertLLM2/AssertLLM2/assertbench/methods/plain_prompt/`, but
 this adapter does not use them for the current Anthropic generation path.

## Secret Loading And Cloud Gate

- Cloud opt-in env var: `ASSERTLLM2_SBY_ENABLE_CLOUD_LLM`
- Blocking values: missing, empty, or `0`
- Permitting value: `1`
- API-key env var: `ANTHROPIC_API_KEY`
- `.env` loading: the AssertLLM2-SBY CLI loads the repository-root `.env` once
  with `python-dotenv` and `override=False`. `vendor/llm_client.py`
  still reads the resulting process environment.
- Secret storage policy: API-key values must not be written to config, manifests,
  prompts, logs, or reports. Adapter config redaction covers keys matching API
  key, token, secret, password, and credential patterns.

With `ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1` but no `ANTHROPIC_API_KEY`, generation still
fails closed before any network request.

## Formal Runtime

- Formal backend status in `config/assertllm2_sby.yaml`: `implemented: true`
- Formal judge: Yosys, SymbiYosys, `yosys-smtbmc`, and Z3
- Default engine: `smtbmc`
- Default solver: `z3`
- Default BMC depth: `8`
- Default prove depth: `8`
- Default cover depth: `6`
- Default timeout: `30` seconds
- Default jobs: `1`
- Default trace generation: `true`
- Synthetic validation command: `python -m assertllm2_sby formal-self-test`
- Current role split: Anthropic generates candidate assertions; SymbiYosys/Yosys/Z3
  classify formal outcomes.
