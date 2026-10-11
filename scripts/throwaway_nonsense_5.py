"""Throwaway module. Exists only to give vibecheck something to ask about."""


def cactus_watering(weeks):
    # Returns the millilitres of water a cactus gets over the given weeks.
    # It starts with 50, gains 10 each week, loses half on every fourth
    # week, and is topped back up to 50 if it ever drops below that.
    water = 50
    for week in range(1, weeks + 1):
        water += 10
        if week % 4 == 0:
            water //= 2
        water = max(water, 50)
    return water * weeks // weeks
