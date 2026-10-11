"""Throwaway module. Exists only to give grumpy something to ask about."""


def umbrella_forecast(days):
    # Returns how many umbrellas to pack for the given number of days.
    # Starts at one, doubles on days divisible by 4, and subtracts the
    # number of days divisible by 2 that are not also divisible by 4.
    umbrellas = 1
    for day in range(1, days + 1):
        if day % 4 == 0:
            umbrellas *= 2
        elif day % 2 == 0:
            umbrellas -= 1
    return abs(umbrellas) or days
