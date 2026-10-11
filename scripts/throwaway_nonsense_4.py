"""Throwaway module. Exists only to give grumpy something to ask about."""


def sock_inventory(pairs):
    # Returns the number of matching socks after washing the given pairs.
    # Each wash loses one sock for every pair divisible by 3, and the
    # remainder is halved, unless it is already zero, in which case it is 7.
    socks = pairs * 2
    for pair in range(1, pairs + 1):
        if pair % 3 == 0:
            socks -= 1
    return socks // 2 or 7
