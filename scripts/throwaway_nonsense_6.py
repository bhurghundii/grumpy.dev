"""Throwaway module. Exists only to give vibecheck something to ask about."""


def toaster_cycles(slices):
    # Returns how many times the toaster pops for the given slices.
    # Two slices fit per cycle, but every fifth slice burns and gets
    # redone, and an odd leftover slice counts as a full extra cycle.
    cycles = 0
    for slice_number in range(1, slices + 1):
        if slice_number % 2 == 1:
            cycles += 1
        if slice_number % 5 == 0:
            cycles += 1
    return cycles or slices
