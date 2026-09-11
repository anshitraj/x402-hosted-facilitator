from __future__ import annotations

from typing import Any


class HostedExactClientSigner:
    """Minimal EVM signer for upstream x402 exact client smoke tests."""

    def __init__(self, private_key: str) -> None:
        from eth_account import Account

        self._private_key = private_key
        self._account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    def sign_typed_data(
        self,
        domain: Any,
        types: dict[str, list[Any]],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        if isinstance(domain, dict):
            domain_dict = domain
        else:
            domain_dict = {
                "name": getattr(domain, "name", None),
                "chainId": getattr(domain, "chain_id", None),
                "verifyingContract": getattr(domain, "verifying_contract", None),
            }
            domain_version = getattr(domain, "version", None)
            if domain_version:
                domain_dict["version"] = domain_version
        domain_dict = {key: value for key, value in domain_dict.items() if value is not None}

        domain_field_map = {
            "name": {"name": "name", "type": "string"},
            "version": {"name": "version", "type": "string"},
            "chainId": {"name": "chainId", "type": "uint256"},
            "verifyingContract": {"name": "verifyingContract", "type": "address"},
            "salt": {"name": "salt", "type": "bytes32"},
        }
        full_types: dict[str, list[dict[str, str]]] = {
            "EIP712Domain": [
                domain_field_map[key] for key in domain_dict if key in domain_field_map
            ]
        }
        for type_name, fields in types.items():
            full_types[type_name] = [
                {"name": field.name, "type": field.type} if hasattr(field, "name") else field
                for field in fields
            ]

        message_copy = dict(message)
        if "nonce" in message_copy and isinstance(message_copy["nonce"], bytes):
            message_copy["nonce"] = "0x" + message_copy["nonce"].hex()

        signable = encode_typed_data(
            full_message={
                "types": full_types,
                "primaryType": primary_type,
                "domain": domain_dict,
                "message": message_copy,
            }
        )
        signed = Account.sign_message(signable, self._private_key)
        return bytes(signed.signature)
