#!/usr/bin/env python3
from __future__ import annotations

import os

from hosted_facilitator.hosted import (
    create_hosted_exact_facilitator_app_from_env,
)
from hosted_facilitator.hosted.runner import load_hosted_exact_runtime_config_from_env

config = load_hosted_exact_runtime_config_from_env().exact_config
initialize_storage = os.environ.get(
    "OMNICLAW_HOSTED_INITIALIZE_STORAGE", "true"
).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

if os.environ.get("OMNICLAW_HOSTED_DEFAULT_API_KEY", "").strip():
    raise RuntimeError(
        "OMNICLAW_HOSTED_DEFAULT_API_KEY is not supported. Create seller-scoped API keys "
        "through the control plane so every key is bound to an explicit payment profile."
    )

app = create_hosted_exact_facilitator_app_from_env(
    seller_accounts=None,
    initialize_storage=initialize_storage,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)
