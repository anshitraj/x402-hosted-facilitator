from __future__ import annotations

from typing import Any


def patch_x402_web3_compat() -> None:
    """
    Patch the web3 middleware symbol expected by x402's current EVM signer import.

    The x402 Python package expects `web3.middleware.ExtraDataToPOAMiddleware`, while
    the web3 build in this environment exposes `geth_poa_middleware`.
    """
    try:
        import web3.middleware as middleware

        if hasattr(middleware, "ExtraDataToPOAMiddleware"):
            return

        from web3.middleware.geth_poa import geth_poa_middleware

        middleware.ExtraDataToPOAMiddleware = geth_poa_middleware
    except Exception:
        return


def get_signed_raw_transaction_bytes(signed_tx: Any) -> bytes:
    """
    Return raw signed transaction bytes across eth-account naming variants.

    Different eth-account versions expose either `rawTransaction` or
    `raw_transaction`.
    """
    raw_tx = getattr(signed_tx, "rawTransaction", None) or getattr(
        signed_tx,
        "raw_transaction",
        None,
    )
    if raw_tx is None:
        raise AttributeError("SignedTransaction has neither rawTransaction nor raw_transaction")
    return raw_tx
