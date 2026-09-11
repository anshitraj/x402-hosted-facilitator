"""
EIP-3009 TransferWithAuthorization signing for Circle Gateway.

This module implements the cryptographic primitives needed for Circle's
batched nanopayments. The buyer signs an EIP-3009 TransferWithAuthorization
message, which authorizes Circle Gateway to transfer USDC from their
Gateway balance to the seller.

CRITICAL SECURITY NOTES:
    - The verifyingContract MUST be the Gateway Wallet contract address,
      NOT the USDC token address. These are different contracts.
    - The EIP-712 domain name MUST be 'GatewayWalletBatched', NOT 'USD Coin'.
      This is a common mistake that produces invalid signatures.
    - validBefore MUST be at least 3 days (259200 seconds) in the future.
    - The nonce MUST be cryptographically random (32 bytes).
    - The private key MUST be an EOA private key (64 hex chars, 32 bytes).

References:
    - EIP-3009: https://eips.ethereum.org/EIPS/eip-3009
    - EIP-712: https://eips.ethereum.org/EIPS/eip-712
    - Circle Gateway: https://developers.circle.com/gateway/nanopayments
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from eth_account import Account
from eth_account.messages import encode_typed_data

from hosted_facilitator.nanopayments.constants import (
    CIRCLE_BATCHING_NAME,
    CIRCLE_BATCHING_VERSION,
    DEFAULT_VALID_BEFORE_SECONDS,
    MIN_VALID_BEFORE_SECONDS,
)
from hosted_facilitator.nanopayments.exceptions import (
    InvalidPrivateKeyError,
    SigningError,
)
from hosted_facilitator.nanopayments.types import (
    EIP3009Authorization,
    PaymentPayload,
    PaymentPayloadInner,
    PaymentRequirementsKind,
)

if TYPE_CHECKING:
    pass

# =============================================================================
# EIP-712 TYPE DEFINITIONS
# =============================================================================

# EIP-712 Domain separator type
EIP712_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

# EIP-3009 TransferWithAuthorization type
EIP3009_TYPE = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]

# Primary type for EIP-712 signing
PRIMARY_TYPE = "TransferWithAuthorization"


# =============================================================================
# EIP-712 DOMAIN CONSTRUCTION
# =============================================================================


def build_eip712_domain(
    chain_id: int,
    verifying_contract: str,
    name: str = CIRCLE_BATCHING_NAME,
    version: str = CIRCLE_BATCHING_VERSION,
) -> dict:
    """
    Build the EIP-712 domain separator for Circle Gateway signing.

    CRITICAL: The name MUST be 'GatewayWalletBatched' for Circle Gateway.
    Using 'USD Coin' (the standard USDC EIP-712 domain) will produce
    signatures that Circle Gateway rejects.

    Args:
        chain_id: The chain ID as an integer (e.g., 5042002 for Arc Testnet).
            NOT the CAIP-2 string (e.g., 'eip155:5042002').
        verifying_contract: The Gateway Wallet contract address.
            Fetched from /v1/x402/supported or from requirements.extra.verifyingContract.
            This is NOT the USDC token address — they are different contracts.
        name: The EIP-712 domain name. MUST be 'GatewayWalletBatched' for Circle.
        version: The domain version. Typically '1'.

    Returns:
        The EIP-712 domain dictionary for use with eth_account.

    Raises:
        SigningError: If any parameter is invalid.
    """
    if not verifying_contract:
        raise SigningError(
            "verifying_contract cannot be empty",
            code="MISSING_VERIFYING_CONTRACT",
        )

    if not verifying_contract.startswith("0x"):
        raise SigningError(
            f"verifying_contract must be a hex address: {verifying_contract}",
            code="INVALID_ADDRESS_FORMAT",
        )

    if chain_id <= 0:
        raise SigningError(
            f"chain_id must be positive: {chain_id}",
            code="INVALID_CHAIN_ID",
        )

    return {
        "name": name,
        "version": version,
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }


def build_eip712_message(
    from_address: str,
    to_address: str,
    value: int,
    valid_after: int = 0,
    valid_before: int | None = None,
    nonce: str | None = None,
) -> dict:
    """
    Build the EIP-712 message for EIP-3009 TransferWithAuthorization.

    Args:
        from_address: The buyer's EOA address (the payer).
        to_address: The seller's address (from 'payTo' in requirements).
        value: Payment amount in USDC atomic units (6 decimals).
            e.g., 1000 = 0.001 USDC = $0.001.
        valid_after: Unix timestamp when authorization becomes valid.
            0 means immediately valid.
        valid_before: Unix timestamp when authorization expires.
            MUST be at least 3 days (259200 seconds) from now for Gateway.
            If None, defaults to now + DEFAULT_VALID_BEFORE_SECONDS (4 days).
        nonce: Random 32-byte hex string (with 0x prefix).
            If None, a new random nonce is generated.
            Must be unique per (from, to) pair for replay protection.

    Returns:
        The EIP-712 message dictionary.

    Raises:
        SigningError: If any parameter is invalid.
    """
    # Validate addresses
    if not from_address.startswith("0x"):
        raise SigningError(
            f"from_address must be a hex address: {from_address}",
            code="INVALID_FROM_ADDRESS",
        )

    if not to_address.startswith("0x"):
        raise SigningError(
            f"to_address must be a hex address: {to_address}",
            code="INVALID_TO_ADDRESS",
        )

    if from_address.lower() == to_address.lower():
        raise SigningError(
            "from_address and to_address cannot be the same (self-transfer)",
            code="SELF_TRANSFER",
        )

    # Validate value
    if value < 0:
        raise SigningError(
            f"value must be non-negative: {value}",
            code="INVALID_VALUE",
        )

    # Handle valid_before
    if valid_before is None:
        valid_before = int(time.time()) + DEFAULT_VALID_BEFORE_SECONDS
    else:
        min_valid_before = int(time.time()) + MIN_VALID_BEFORE_SECONDS
        if valid_before < min_valid_before:
            raise SigningError(
                f"valid_before must be at least {MIN_VALID_BEFORE_SECONDS} seconds "
                f"({MIN_VALID_BEFORE_SECONDS // 86400} days) in the future. "
                f"Got: {valid_before}, minimum required: {min_valid_before}",
                code="VALID_BEFORE_TOO_SOON",
            )

    # Handle nonce
    if nonce is None:
        nonce = "0x" + os.urandom(32).hex()
    else:
        # Validate nonce format
        if not nonce.startswith("0x"):
            raise SigningError(
                f"nonce must start with 0x: {nonce}",
                code="INVALID_NONCE_FORMAT",
            )

        nonce_hex = nonce[2:]  # Remove 0x prefix
        if len(nonce_hex) != 64:
            raise SigningError(
                f"nonce must be 32 bytes (64 hex chars), got {len(nonce_hex) // 2} bytes",
                code="INVALID_NONCE_LENGTH",
            )

        # Validate it's valid hex
        try:
            int(nonce_hex, 16)
        except ValueError:
            raise SigningError(
                "nonce must be a valid hex string",
                code="INVALID_NONCE_HEX",
            ) from None

    return {
        "from": from_address,
        "to": to_address,
        "value": value,
        "validAfter": valid_after,
        "validBefore": valid_before,
        "nonce": nonce,
    }


def build_eip712_structured_data(domain: dict, message: dict) -> dict:
    """
    Build the complete EIP-712 structured data for signing.

    This combines the domain separator and message into the full
    EIP-712 structured data format required by eth_account.

    Args:
        domain: EIP-712 domain separator (from build_eip712_domain).
        message: EIP-712 message (from build_eip712_message).

    Returns:
        Complete EIP-712 structured data dictionary for eth_account.
    """
    return {
        "domain": domain,
        "types": {
            "EIP712Domain": EIP712_DOMAIN_TYPE,
            "TransferWithAuthorization": EIP3009_TYPE,
        },
        "primaryType": PRIMARY_TYPE,
        "message": message,
    }


# =============================================================================
# EIP-3009 SIGNER
# =============================================================================


class EIP3009Signer:
    """
    Signs EIP-3009 TransferWithAuthorization messages.

    This class handles the complete flow from payment requirements
    to signed payload ready for Circle Gateway settlement.

    Usage:
        >>> signer = EIP3009Signer(private_key="0xabc...")
        >>> signer.address  # Get the EOA address
        '0xBuyer123...'
        >>> payload = signer.sign_transfer_with_authorization(
        ...     requirements=payment_requirements,
        ...     amount_atomic=1000,
        ... )
        >>> # payload is ready for Gateway settlement

    Security Notes:
        - Private key is stored in memory for the lifetime of the signer
        - Never log or expose the private key
        - Dispose of the signer when done to clear memory
        - In production, keys should be stored encrypted and only
          decrypted when needed (legacy vault)
    """

    def __init__(self, private_key: str) -> None:
        """
        Initialize the signer with an EOA private key.

        Args:
            private_key: The EOA private key.
                Can be with or without '0x' prefix.
                Must be exactly 64 hex characters (32 bytes).

        Raises:
            InvalidPrivateKeyError: If the key is invalid.
        """
        # Normalize: remove 0x prefix if present
        if private_key.startswith("0x"):
            private_key = private_key[2:]

        # Validate length
        if len(private_key) != 64:
            raise InvalidPrivateKeyError(
                f"Private key must be 64 hex chars (32 bytes), got {len(private_key)}"
            )

        # Validate it's valid hex
        try:
            int(private_key, 16)
        except ValueError:
            raise InvalidPrivateKeyError("Private key contains invalid hex characters") from None

        self._private_key: str = private_key

        # Derive address from private key
        try:
            self._account = Account.from_key("0x" + private_key)
            self._address: str = self._account.address
        except Exception as e:
            raise InvalidPrivateKeyError(f"Failed to derive address: {e}") from None

    def __repr__(self) -> str:
        """Safe representation without private-key material."""
        return f"EIP3009Signer(address={self._address})"

    def __del__(self) -> None:
        """Attempt to clear sensitive data on deletion."""
        # Note: Python doesn't guarantee this runs, but it's a best effort
        if hasattr(self, "_private_key"):
            self._private_key = "\x00" * 64  # Overwrite with zeros

    @property
    def address(self) -> str:
        """
        The EOA address derived from the private key.

        This is the address that will be recorded as the payer
        in Circle Gateway's settlement records.
        """
        return self._address

    @property
    def _raw_key(self) -> str:
        """
        INTERNAL: The raw private key hex string.

        This is for use by GatewayWalletManager for on-chain transaction signing.
        Do NOT expose this to agents. Use the signing interface instead.
        """
        return self._private_key

    @property
    def raw_key(self) -> str:
        """
        The raw private key hex string (with 0x prefix).

        This is used for on-chain operations like deposit/withdraw.
        """
        return self._private_key

    def sign_transfer_with_authorization(
        self,
        requirements: PaymentRequirementsKind,
        amount_atomic: int | None = None,
        valid_before: int | None = None,
        nonce: str | None = None,
    ) -> PaymentPayload:
        """
        Sign an EIP-3009 TransferWithAuthorization for the given requirements.

        This is the primary method for creating a nanopayment.

        Args:
            requirements: The payment requirements from the server's 402 response.
                Must contain the GatewayWalletBatched scheme.
                Contains: network (CAIP-2), payTo address, amount, verifyingContract.
            amount_atomic: Payment amount in USDC atomic units.
                If None, uses the amount from requirements.
                If provided, MUST match or be <= requirements.amount.
            valid_before: Unix timestamp for expiration.
                Must be at least 3 days from now.
                If None, defaults to 4 days from now.
            nonce: Random 32-byte hex string for replay protection.
                If None, a new random nonce is generated.

        Returns:
            A PaymentPayload ready for base64 encoding and
            inclusion in the PAYMENT-SIGNATURE header.

        Raises:
            SigningError: If requirements are invalid or signing fails.
            UnsupportedSchemeError: If requirements aren't GatewayWalletBatched.
            MissingVerifyingContractError: If verifyingContract is missing.
        """
        # Validate scheme
        if requirements.extra.name != CIRCLE_BATCHING_NAME:
            from hosted_facilitator.nanopayments.exceptions import UnsupportedSchemeError

            raise UnsupportedSchemeError(requirements.extra.name)

        # Get verifying contract
        verifying_contract = requirements.extra.verifying_contract
        if not verifying_contract:
            from hosted_facilitator.nanopayments.exceptions import (
                MissingVerifyingContractError,
            )

            raise MissingVerifyingContractError()

        # Determine amount
        if amount_atomic is None:
            amount_atomic = int(requirements.amount)
        else:
            # Validate amount doesn't exceed requirement
            required = int(requirements.amount)
            if amount_atomic > required:
                raise SigningError(
                    f"amount_atomic ({amount_atomic}) exceeds required amount ({required})",
                    code="AMOUNT_EXCEEDS_REQUIREMENT",
                )

        # Parse chain ID from CAIP-2 format
        # Format: "eip155:<chainId>"
        network = requirements.network
        if not network.startswith("eip155:"):
            from hosted_facilitator.nanopayments.exceptions import UnsupportedNetworkError

            raise UnsupportedNetworkError(network)

        try:
            chain_id = int(network.split(":")[1])
        except (IndexError, ValueError):
            from hosted_facilitator.nanopayments.exceptions import UnsupportedNetworkError

            raise UnsupportedNetworkError(network) from None

        # Build EIP-712 domain
        domain = build_eip712_domain(
            chain_id=chain_id,
            verifying_contract=verifying_contract,
            name=requirements.extra.name,
            version=requirements.extra.version,
        )

        # Build EIP-712 message
        message = build_eip712_message(
            from_address=self._address,
            to_address=requirements.pay_to,
            value=amount_atomic,
            valid_before=valid_before,
            nonce=nonce,
        )

        # Build full structured data
        structured_data = build_eip712_structured_data(domain, message)

        # Sign the message
        try:
            signable = encode_typed_data(full_message=structured_data)
            signed = self._account.sign_message(signable)
            signature = signed.signature.hex()
        except Exception as e:
            raise SigningError(
                f"Failed to sign EIP-712 message: {e}",
                code="SIGN_FAILED",
            ) from None

        # Build authorization
        authorization = EIP3009Authorization.create(
            from_address=self._address,
            to=requirements.pay_to,
            value=str(amount_atomic),
            valid_before=message["validBefore"],
            nonce=message["nonce"],
        )

        # Build payload
        return PaymentPayload(
            x402_version=2,
            scheme=requirements.scheme,
            network=requirements.network,
            payload=PaymentPayloadInner(
                signature="0x" + signature,
                authorization=authorization,
            ),
        )

    def verify_signature(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirementsKind,
    ) -> bool:
        """
        Verify a signature locally (for testing/debugging).

        This does NOT call Circle Gateway — use NanopaymentClient.verify()
        for production verification.

        Args:
            payload: The signed payment payload.
            requirements: The payment requirements that were signed.

        Returns:
            True if the signature is valid and matches the requirements.

        Raises:
            SigningError: If verification fails.
        """
        # Parse chain ID
        chain_id = int(requirements.network.split(":")[1])

        # Build domain
        domain = build_eip712_domain(
            chain_id=chain_id,
            verifying_contract=requirements.extra.verifying_contract,
        )

        # Build message matching what was signed
        message = {
            "from": payload.payload.authorization.from_address,
            "to": payload.payload.authorization.to,
            "value": int(payload.payload.authorization.value),
            "validAfter": int(payload.payload.authorization.valid_after),
            "validBefore": int(payload.payload.authorization.valid_before),
            "nonce": payload.payload.authorization.nonce,
        }

        # Build structured data
        structured_data = build_eip712_structured_data(domain, message)

        # Recover signer
        try:
            signable = encode_typed_data(full_message=structured_data)
            recovered = Account.recover_message(signable, signature=payload.payload.signature)
        except Exception as e:
            raise SigningError(f"Signature recovery failed: {e}") from None

        # Verify recovered address matches
        return recovered.lower() == self._address.lower()


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def parse_caip2_chain_id(network: str) -> int:
    """
    Parse chain ID from CAIP-2 format.

    Args:
        network: CAIP-2 network identifier, e.g., 'eip155:5042002'.

    Returns:
        The chain ID as an integer.

    Raises:
        ValueError: If the format is invalid.

    Example:
        >>> parse_caip2_chain_id("eip155:5042002")
        5042002
    """
    if not network.startswith("eip155:"):
        raise ValueError(f"Invalid CAIP-2 format: {network}. Expected 'eip155:<chainId>'.")

    try:
        return int(network.split(":")[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"Invalid chain ID in CAIP-2 format: {network}") from e


def generate_nonce() -> str:
    """
    Generate a cryptographically random 32-byte nonce.

    Uses os.urandom for cryptographic security.

    Returns:
        A 32-byte random value as a hex string with '0x' prefix.

    Example:
        >>> nonce = generate_nonce()
        >>> len(nonce)  # 0x + 64 hex chars = 66
        66
    """
    return "0x" + os.urandom(32).hex()


def compute_valid_before(
    seconds_from_now: int = DEFAULT_VALID_BEFORE_SECONDS,
) -> int:
    """
    Compute a validBefore timestamp.

    Args:
        seconds_from_now: Seconds until expiration. Default 4 days.

    Returns:
        Unix timestamp for validBefore.

    Example:
        >>> # Valid for 4 days from now
        >>> valid_before = compute_valid_before()
        >>> # Valid for exactly 3 days (minimum for Gateway)
        >>> valid_before = compute_valid_before(259200)
    """
    return int(time.time()) + seconds_from_now


def generate_eoa_keypair() -> tuple[str, str]:
    """
    Generate a new EOA private key and address.

    WARNING: This is for testing and setup purposes only.
    In production, keys should be generated securely and stored encrypted.

    Returns:
        Tuple of (private_key_hex, address).
        private_key_hex: 66 chars, '0x' prefix + 64 hex chars.
        address: The derived EOA address (checksummed).

    Example:
        >>> key, address = generate_eoa_keypair()
        >>> print(f"Fund this address: {address}")
        Fund this address: 0x1234...
    """
    account = Account.create()
    return "0x" + account.key.hex(), account.address
