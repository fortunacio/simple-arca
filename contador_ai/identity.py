from __future__ import annotations


def normalize_cuit(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def is_valid_cuit(value: str) -> bool:
    cuit = normalize_cuit(value)
    if len(cuit) != 11 or len(set(cuit)) == 1:
        return False
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    checksum = sum(int(digit) * weight for digit, weight in zip(cuit[:10], weights))
    verifier = 11 - (checksum % 11)
    if verifier == 11:
        verifier = 0
    elif verifier == 10:
        verifier = 9
    return verifier == int(cuit[-1])


def mask_cuit(value: str) -> str:
    cuit = normalize_cuit(value)
    if len(cuit) != 11:
        return "CUIT inválido"
    return f"{cuit[:2]}-******{cuit[8:10]}-{cuit[-1]}"
