"""The suite's seeded scenario, reimplemented from wire.ts.

The scenario is a pure function of SCENARIO_SEED, so the forge can derive
the token owner's login (and the stranger's, for 404 decisions) without the
env telling it. Verified against the kit's own buildScenario(): viewer
'adaiversen' (id 1000), stranger 'olgabauer', org 'faint-meadow-org'.
"""
import re

MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo",
    "iris", "jonas", "kira", "lars", "mira", "nico", "olga", "pavel",
    "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
]
LAST_NAMES = [
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia",
    "holm", "iversen", "jansen", "keller", "lind", "moreau", "novak",
    "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega",
    "weber", "xu", "yilmaz", "zimmer",
]


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & MASK
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return (z ^ (z >> 31)) & MASK


class _Rng:
    def __init__(self, seed):
        s = seed & MASK
        self.state = s if s != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & MASK
        x = (x ^ ((x << 25) & MASK)) & MASK
        x = (x ^ (x >> 27)) & MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, mx):
        if mx == 1:
            return 0
        limit = 4294967296 - (4294967296 % mx)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % mx

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_seed_for(label):
    return _mix64(SCENARIO_SEED ^ _fnv1a64(label))


def _generate_users(count=12):
    rng = _Rng(_scenario_seed_for("users"))
    used = set()
    users = []
    for i in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        login = re.sub(r"[^a-z0-9-]", "", first + last)
        suffix = 2
        base = login
        while login in used:
            login = "%s-%d" % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({"id": 1000 + i, "login": login,
                      "name": "%s %s" % (first, last),
                      "email": "%s@example.invalid" % login})
    return users


_USERS = _generate_users()
VIEWER = _USERS[0]
TEAMMATE = _USERS[1]
STRANGER = _USERS[2]
VIEWER_LOGIN = VIEWER["login"]
VIEWER_ID = VIEWER["id"]
VIEWER_NAME = VIEWER["name"]
VIEWER_EMAIL = VIEWER["email"]
