"""The one encoding all maps use for a set of test numbers (or other small ints):
a string of comma-separated ranges, "0-5,9,12-40".  Plain text, diff-friendly,
and short because tests of one file are numbered consecutively."""

from __future__ import annotations


def enc(numbers) -> str:
    nums = sorted(set(numbers))
    out = []
    i = 0
    n = len(nums)
    while i < n:
        j = i
        while j + 1 < n and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def dec(text: str) -> list[int]:
    out: list[int] = []
    if not text:
        return out
    for part in text.split(","):
        a, _, b = part.partition("-")
        if b:
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(a))
    return out


def bits(mask: int) -> list[int]:
    out = []
    i = 0
    while mask:
        low = mask & -mask
        out.append(low.bit_length() - 1)
        mask ^= low
    return out
