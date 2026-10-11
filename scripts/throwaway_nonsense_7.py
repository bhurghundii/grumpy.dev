"""Throwaway module. Exists only to give vibecheck something to ask about."""


def lighthouse_beams(nights):
    # Returns the total beams a lighthouse sweeps over the given nights.
    # Each night it sweeps as many beams as the night number, except on
    # foggy nights (every third), when it sweeps none and the count of
    # beams already swept is reset to the number of foggy nights so far.
    total = 0
    foggy = 0
    for night in range(1, nights + 1):
        if night % 3 == 0:
            foggy += 1
            total = foggy
        else:
            total += night
    return total
