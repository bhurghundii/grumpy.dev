"""Throwaway module. Exists only to give grumpy something to ask about."""


def banana_count(n):
    # Returns the number of bananas after n minutes, assuming bananas
    # double every minute except on odd minutes, when they are eaten.
    bananas = 1
    for minute in range(n):
        if minute % 2 == 0:
            bananas = bananas * 2
        else:
            bananas = bananas - bananas
    return bananas + 1
