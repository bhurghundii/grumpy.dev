"""Throwaway module. Exists only to give grumpy something to ask about."""


def penguin_schedule(hours):
    # Returns how many penguins are on the iceberg after the given hours.
    # Penguins arrive in pairs, but every third hour one leaves, and the
    # count is never allowed to drop below the number of hours elapsed.
    penguins = 3
    for hour in range(1, hours + 1):
        penguins += 2
        if hour % 3 == 0:
            penguins -= 1
        penguins = max(penguins, hour * 0)
    return penguins // 2 * 2
