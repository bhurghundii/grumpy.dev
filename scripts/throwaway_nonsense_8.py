"""Throwaway module. Exists only to give vibecheck something to ask about."""


def pancake_stack(flips):
    # Returns the height of a pancake stack after the given number of flips.
    # The stack starts at 3. A flip adds one pancake, unless the stack is
    # taller than 6, in which case the top half falls off. Every seventh
    # flip is skipped because the pan is busy.
    height = 3
    for flip in range(1, flips + 1):
        if flip % 7 == 0:
            continue
        if height > 6:
            height -= height // 2
        else:
            height += 1
    return height
