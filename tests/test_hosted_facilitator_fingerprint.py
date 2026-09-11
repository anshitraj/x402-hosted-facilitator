from __future__ import annotations

from hosted_facilitator.hosted.fingerprint import payment_fingerprint


def _payload(
    from_address: str = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    *,
    nonce: str = "0x" + "11" * 32,
    value=250000,
) -> dict:
    return {
        "x402Version": 2,
        "payload": {
            "signature": "0xsig",
            "authorization": {
                "from": from_address,
                "to": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "value": value,
                "validAfter": "00",
                "validBefore": "9999999999",
                "nonce": nonce,
            },
        },
        "accepted": {
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0X3600000000000000000000000000000000000000",
            "amount": "250000.0",
            "payTo": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        },
    }


def _requirements(resource: str = "https://seller.example.com/compute") -> dict:
    return {
        "scheme": "exact",
        "network": "eip155:5042002",
        "asset": "0x3600000000000000000000000000000000000000",
        "amount": "250000",
        "payTo": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "resource": resource,
    }


def test_payment_fingerprint_is_stable_for_case_changes():
    first = payment_fingerprint(
        payment_payload=_payload("0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        payment_requirements=_requirements(),
        provider="exact_evm",
    )
    second = payment_fingerprint(
        payment_payload=_payload(
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            nonce="0X" + "11" * 32,
            value="250000.00",
        ),
        payment_requirements={
            **_requirements(),
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
        provider="exact_evm",
    )

    assert first == second


def test_payment_fingerprint_changes_for_required_fields():
    baseline = payment_fingerprint(
        payment_payload=_payload(),
        payment_requirements=_requirements(),
        provider="exact_evm",
    )

    mutations = [
        {"provider": "circle_gateway"},
        {
            "payment_payload": {
                **_payload(),
                "accepted": {**_payload()["accepted"], "amount": "250001"},
            }
        },
        {
            "payment_payload": {
                **_payload(),
                "payload": {
                    **_payload()["payload"],
                    "authorization": {
                        **_payload()["payload"]["authorization"],
                        "nonce": "0x" + "22" * 32,
                    },
                },
            }
        },
        {
            "payment_payload": {
                **_payload(),
                "accepted": {
                    **_payload()["accepted"],
                    "payTo": "0xcccccccccccccccccccccccccccccccccccccccc",
                },
            }
        },
    ]

    for mutation in mutations:
        assert (
            payment_fingerprint(
                payment_payload=mutation.get("payment_payload", _payload()),
                payment_requirements=mutation.get("payment_requirements", _requirements()),
                provider=mutation.get("provider", "exact_evm"),
            )
            != baseline
        )


def test_payment_fingerprint_is_stable_when_resource_changes():
    first = payment_fingerprint(
        payment_payload=_payload(),
        payment_requirements=_requirements("https://seller.example.com/compute-a"),
        provider="exact_evm",
    )
    second = payment_fingerprint(
        payment_payload=_payload(),
        payment_requirements=_requirements("https://seller.example.com/compute-b"),
        provider="exact_evm",
    )

    assert first == second
