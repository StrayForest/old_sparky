PLAYTIME_OPTIONS = ["0-500", "501-1000", "1001-1500", "1501-2000", "2001-3000", "3000+"]
ROLE_OPTIONS = ["Carry", "Semi-Carry", "Support", "Semi-Support"]
CAPTAIN_PRIORITY_OPTIONS = {
    "yes": "Yes",
    "no": "No",
    "neutral": "Neutral",
}
RANKS = [
    "Eternus",
    "Ascendant",
    "Phantom",
    "Oracle",
    "Emissary",
    "Ritualist",
    "Mystic",
    "Sentinel",
    "Acolyte",
    "Seeker",
    "Initiate",
]
CAPTAIN_PRIORITY_ELIGIBLE_RANKS = set(RANKS)

POOL_LIST = [
    "Abrams",
    "Apollo",
    "Bebop",
    "Billy",
    "Calico",
    "Celeste",
    "The Doorman",
    "Drifter",
    "Dynamo",
    "Graves",
    "Grey Talon",
    "Haze",
    "Holliday",
    "Infernus",
    "Ivy",
    "Kelvin",
    "Lady Geist",
    "Lash",
    "McGinnis",
    "Mina",
    "Mirage",
    "Mo & Krill",
    "Paradox",
    "Paige",
    "Pocket",
    "Rem",
    "Seven",
    "Shiv",
    "Silver",
    "Sinclair",
    "Venator",
    "Victor",
    "Vindicta",
    "Viscous",
    "Vyper",
    "Warden",
    "Wraith",
    "Yamato",
]

BASE = 1.4
RANK_POWER = {rank: index for index, rank in enumerate(reversed(RANKS))}
